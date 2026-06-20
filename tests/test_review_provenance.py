"""Provenance fields stamped on review results (Track A, phases 1.3 and 2.4).

Two additive guarantees:

  * playbook_version = {id, hash, label} is stamped on EVERY review result, by
    both the deterministic and the AI-first engine, at the single choke point
    (review_engine.review_nda_with_active_engine). Its hash is the same stable
    content hash carried by playbook_runtime.active_hash, so the approval gate can
    detect staleness by comparing playbook_version.hash to the current published
    hash. The hash is stable across re-reads and changes when the Playbook changes.

  * redline_rationale = {explanation, basis:{quote, paragraph_id}} is attached to
    every clause that produces a redline (and only those), explaining WHY the edit
    is proposed, sourced from the Playbook clause + the clause's grounded citation.

We inject explicit runtimes/playbook paths rather than patching module globals,
because the runtime helper's default playbook_path binds to the production path at
import time. See [[playbook-path-default-arg-test-gotcha]].
"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from nda_automation import review_engine
from nda_automation.ai_assessor import (
    InMemoryAssessmentReviewer,
    assess_nda_with_ai,
    stub_ai_assessment_response,
)
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.checker import REVIEW_ENGINE_VERSION, load_playbook, review_nda
from nda_automation.redline_rationale import REDLINE_RATIONALE_VERSION
from nda_automation.routes import playbook as playbook_routes
from nda_automation import playbook_runtime


def _runtime(**overrides):
    runtime = {
        "active_version_id": "pbv_20260605T000000Z_abc123def456",
        "active_hash": "sha256:" + "a" * 64,
        "playbook_name": "Aspora NDA Playbook",
        "playbook_version": "2026.06",
        "published_at": "2026-06-05T00:00:00+00:00",
        "published_by": "legal-admin",
        "source": "publish",
    }
    runtime.update(overrides)
    return runtime


def _stub_review():
    return {
        "review_engine_version": REVIEW_ENGINE_VERSION,
        "review_mode": "ai_first_compat",
        "clauses": [],
        "review_state": {},
        "ai_first_review": {"status": "completed"},
    }


# A document that violates several Playbook positions so multiple clauses redline:
# foreign governing law, perpetual survival, a prohibited non-circumvention term,
# and a weak Confidential Information definition. "MUTUAL" keeps mutuality passing.
_VIOLATING_NDA = """MUTUAL NON-DISCLOSURE AGREEMENT

1. Confidential Information means any information disclosed by the Disclosing Party.

2. The Receiving Party shall not circumvent the Disclosing Party or deal directly with any introduced party for two years.

3. This Agreement shall be governed by the laws of France.

4. The obligations of confidentiality shall continue in perpetuity.

5. Signed by the parties.
"""


class PlaybookVersionStampTests(unittest.TestCase):
    def test_version_stamp_present_on_deterministic_result(self):
        runtime = _runtime()
        result = review_engine.review_nda_with_active_engine(
            "NDA text",
            deterministic_review_func=lambda text, paragraphs=None: _stub_review(),
            ai_first_review_func=lambda text, paragraphs=None: _stub_review(),
            playbook_runtime_func=lambda: runtime,
        )
        version = result["playbook_version"]
        self.assertEqual(set(version), {"id", "hash", "label"})
        self.assertEqual(version["id"], runtime["active_version_id"])
        self.assertEqual(version["hash"], runtime["active_hash"])
        self.assertEqual(version["label"], "Aspora NDA Playbook v2026.06")

    def test_version_stamp_present_on_ai_first_result(self):
        runtime = _runtime()
        # Force the AI-first branch and confirm the engine stamps the version even
        # though the inner func returns a result without one.
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {review_engine.ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}):
            result = review_engine.review_nda_with_active_engine(
                "NDA text",
                ai_first_review_func=lambda text, paragraphs=None: _stub_review(),
                playbook_runtime_func=lambda: runtime,
            )
        version = result["playbook_version"]
        self.assertEqual(version["hash"], runtime["active_hash"])
        self.assertEqual(version["id"], runtime["active_version_id"])

    def test_version_hash_equals_runtime_active_hash(self):
        # The contract backend-approval relies on: playbook_version.hash is the same
        # value as playbook_runtime.active_hash, so a single hash drives staleness.
        runtime = _runtime()
        result = review_engine.review_nda_with_active_engine(
            "NDA text",
            deterministic_review_func=lambda text, paragraphs=None: _stub_review(),
            ai_first_review_func=lambda text, paragraphs=None: _stub_review(),
            playbook_runtime_func=lambda: runtime,
        )
        self.assertEqual(
            result["playbook_version"]["hash"],
            result["playbook_runtime"]["active_hash"],
        )

    def test_label_falls_back_when_name_or_version_missing(self):
        only_name = _runtime(playbook_name="Solo Playbook", playbook_version="")
        result = review_engine.review_nda_with_active_engine(
            "NDA text",
            deterministic_review_func=lambda text, paragraphs=None: _stub_review(),
            ai_first_review_func=lambda text, paragraphs=None: _stub_review(),
            playbook_runtime_func=lambda: only_name,
        )
        self.assertEqual(result["playbook_version"]["label"], "Solo Playbook")

    def test_ai_first_result_carries_content_version_when_called_directly(self):
        # A direct build_ai_first_review_result call (no runtime) still stamps a
        # content-level version whose hash matches the published snapshot hash.
        playbook = deepcopy(load_playbook())
        result = build_ai_first_review_result("Mutual NDA text.", [], playbook=playbook, verify=False)
        version = result["playbook_version"]
        self.assertEqual(version["hash"], playbook_runtime.playbook_snapshot_hash(playbook))
        # No runtime to assign a published id when called directly.
        self.assertEqual(version["id"], "")
        self.assertTrue(version["label"])


class PlaybookVersionHashStabilityTests(unittest.TestCase):
    """The version hash is stable across re-reads and changes only on publish."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.playbook_path = Path(self._dir.name) / "playbook.json"
        self.original_playbook = deepcopy(load_playbook())
        self.playbook_path.write_text(json.dumps(self.original_playbook), encoding="utf-8")
        playbook_runtime.ensure_active_playbook_runtime(playbook_path=self.playbook_path)

    def _active_runtime(self):
        return playbook_runtime.ensure_active_playbook_runtime(playbook_path=self.playbook_path)

    def _review(self):
        return review_engine.review_nda_with_active_engine(
            "Mutual NDA text.",
            ai_first_review_func=lambda text, paragraphs=None: _stub_review(),
            playbook_runtime_func=self._active_runtime,
        )

    def test_hash_is_stable_across_repeated_reviews(self):
        first = self._review()["playbook_version"]["hash"]
        second = self._review()["playbook_version"]["hash"]
        self.assertEqual(first, second)
        self.assertEqual(first, playbook_runtime.playbook_snapshot_hash(self.original_playbook))

    def test_hash_changes_after_publishing_a_different_playbook(self):
        before = self._review()["playbook_version"]["hash"]

        # Publish a changed Playbook directly (no draft) against the temp path.
        changed = deepcopy(self.original_playbook)
        term = next(c for c in changed["clauses"] if c["id"] == "term_and_survival")
        term["max_term_years"] = 7
        runtime = self._active_runtime()

        class _LoopbackServer:
            # handle_playbook_publish is admin-gated; a loopback host makes the
            # trusted local developer an admin so this provenance test reaches the
            # publish path it is actually exercising.
            server_address = ("127.0.0.1", 0)

        class _Handler:
            def __init__(self, payload):
                self.payload = payload
                self.status = None
                self.response = None
                self.server = _LoopbackServer()

            def _read_json_payload(self):
                return self.payload

            def _send_json(self, payload, *, status=200, send_body=True):
                self.status = status
                self.response = payload

        handler = _Handler({"playbook": changed, "actor": "legal-admin", "expected_active_hash": runtime["active_hash"]})
        playbook_routes.handle_playbook_publish(handler, playbook_path=self.playbook_path)
        self.assertEqual(handler.status, 200, handler.response)

        after = self._review()["playbook_version"]["hash"]
        self.assertNotEqual(before, after)
        self.assertEqual(after, handler.response["active"]["metadata"]["active_hash"])


class RedlineRationaleTests(unittest.TestCase):
    def setUp(self):
        self.result = review_nda(_VIOLATING_NDA, verify=False)
        self.clauses_by_id = {c["id"]: c for c in self.result["clauses"]}

    def _rationale(self, clause_id):
        return self.clauses_by_id[clause_id].get("redline_rationale")

    def test_rationale_only_on_clauses_that_redline(self):
        redlined_clause_ids = {str(edit["clause_id"]) for edit in self.result["redline_edits"]}
        self.assertTrue(redlined_clause_ids, "expected at least one redline in the fixture")
        for clause in self.result["clauses"]:
            has_rationale = clause.get("redline_rationale") is not None
            produced_redline = clause["id"] in redlined_clause_ids
            self.assertEqual(
                has_rationale,
                produced_redline,
                f"{clause['id']}: rationale presence ({has_rationale}) must match redline ({produced_redline})",
            )

    def test_unredlined_clause_has_no_rationale(self):
        # Mutuality is not redlined on this document (it goes to review, not a fix),
        # so it carries no rationale — only redline-producing clauses get one.
        redlined_clause_ids = {str(edit["clause_id"]) for edit in self.result["redline_edits"]}
        self.assertNotIn("mutuality", redlined_clause_ids)
        self.assertIsNone(self._rationale("mutuality"))

    def test_rationale_shape_and_basis_from_edit_when_no_citation(self):
        # The deterministic engine's native clauses carry no structured citation, so
        # the basis falls back to the offending paragraph the edit targets — still a
        # real, grounded {quote, paragraph_id} pointing at the document.
        rationale = self._rationale("governing_law")
        self.assertIsNotNone(rationale)
        self.assertEqual(rationale["version"], REDLINE_RATIONALE_VERSION)
        self.assertIn("action", rationale)
        self.assertIsInstance(rationale["explanation"], str)
        self.assertTrue(rationale["explanation"].strip())
        self.assertIn("Playbook", rationale["explanation"])
        basis = rationale["basis"]
        self.assertEqual(set(basis), {"quote", "paragraph_id"})
        self.assertIn("France", basis["quote"])
        edit = next(e for e in self.result["redline_edits"] if e["clause_id"] == "governing_law")
        self.assertEqual(basis["paragraph_id"], str(edit["paragraph_id"]))
        self.assertEqual(basis["quote"], edit["original_text"])

    def test_explanation_reflects_playbook_requirement(self):
        # The explanation is sourced from the Playbook clause requirement, not the
        # model's free text: the term clause names the five-year survival cap.
        rationale = self._rationale("term_and_survival")
        self.assertIsNotNone(rationale)
        self.assertIn("five years", rationale["explanation"])

    def test_dynamic_prohibited_clause_gets_delete_rationale(self):
        # The deterministic engine never emits the dynamic non_circumvention clause;
        # exercise the dynamic delete path through the AI-first stub reviewer.
        reviewer = InMemoryAssessmentReviewer(response=stub_ai_assessment_response)
        result = assess_nda_with_ai(_VIOLATING_NDA, reviewer=reviewer)
        clause = next(c for c in result["clauses"] if c["id"] == "non_circumvention")
        self.assertEqual(clause["decision"], "fail")
        rationale = clause.get("redline_rationale")
        self.assertIsNotNone(rationale)
        self.assertEqual(rationale["action"], "delete_paragraph")
        # Delete rationale explains the prohibition and grounds in the offending text.
        self.assertIn("prohibit", rationale["explanation"].lower())
        self.assertIn("circumvent", rationale["basis"]["quote"].lower())
        # When the clause carries a grounded citation, the basis sources from it.
        citation = clause.get("citation") or {}
        self.assertEqual(rationale["basis"]["quote"], citation.get("quote"))
        self.assertEqual(rationale["basis"]["paragraph_id"], citation.get("paragraph_id"))


if __name__ == "__main__":
    unittest.main()
