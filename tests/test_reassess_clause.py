"""Unit tests for the single-clause re-assessment endpoint.

Coverage:
- Owner-scoping enforced: attacker cannot reassess a matter they don't own.
- A valid request re-assesses the specified clause and returns an updated verdict.
- Cross-tenant denial: matter owned by user A cannot be reassessed by user B.
- Missing/invalid inputs return appropriate 400 / 404 responses.
- Telemetry counter incremented on success.

All AI calls use the deterministic stub (NDA_AI_ASSESSMENT_STUB=1 is set in
conftest.py), so no live API key is needed.
"""
from __future__ import annotations

import unittest

from nda_automation import telemetry
from nda_automation.ai_first_review import ReassessClauseError, reassess_single_clause
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.review_engine import review_nda_with_active_engine
from nda_automation.routes.review import handle_reassess_clause
from nda_automation.triage import triage_review_result


# ------------------------------------------------------------------ #
# Shared fixtures
# ------------------------------------------------------------------ #

SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    '"Confidential Information" means non-public business, financial information disclosed by either party.',
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations shall survive for a period of five (5) years.",
    "Each party remains free to deal with third parties outside the Purpose of this Agreement.",
    "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
])


def _seed_matter(repository: InMemoryMatterRepository, *, owner_user_id: str = "alice@example.com") -> dict:
    """Create a matter in the repo with a stub AI-first review result."""
    review_result = review_nda_with_active_engine(SOURCE_TEXT)
    triage = triage_review_result(review_result)
    return repository.create_matter(
        source_filename="nda.docx",
        document_bytes=b"fake-docx-bytes",
        extracted_text=SOURCE_TEXT,
        review_result=review_result,
        triage=triage,
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id=owner_user_id,
    )


class _FakeHandler:
    """Minimal stand-in for NdaAutomationHandler; captures the response."""

    def __init__(self, *, current_user_id: str, body: dict | None = None, repository=None):
        self.current_user_id = current_user_id
        self.current_user = {"id": current_user_id, "email": current_user_id} if current_user_id else None
        self._body = body
        self.matter_repository = repository
        self.status = None
        self.json = None

    def _read_json_payload(self):
        return self._body

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.json = payload


# ------------------------------------------------------------------ #
# Core reassess_single_clause unit tests (no HTTP layer)
# ------------------------------------------------------------------ #

class ReassessSingleClauseTests(unittest.TestCase):
    """Directly test the core reassess_single_clause function."""

    def test_returns_updated_clause_result_for_known_clause(self):
        result = reassess_single_clause("mutuality", SOURCE_TEXT)
        self.assertIn("id", result)
        self.assertEqual(result["id"], "mutuality")
        self.assertIn("decision", result)
        self.assertIn(result["decision"], ("pass", "fail", "review"))
        self.assertIn("reassess_metadata", result)
        self.assertEqual(result["reassess_metadata"]["clause_id"], "mutuality")
        self.assertEqual(result["reassess_metadata"]["feature"], "review")

    def test_returns_updated_clause_result_for_governing_law(self):
        result = reassess_single_clause("governing_law", SOURCE_TEXT)
        self.assertEqual(result["id"], "governing_law")
        self.assertIn("decision", result)
        # England and Wales is an approved law — should not be forced to fail.
        self.assertNotEqual(result.get("reason_code"), "unapproved_governing_law")

    # NOTE: the deterministic governing-law backstop was removed once the primary AI
    # proved it reliably FAILs an unapproved jurisdiction on its own (see the
    # key-gated real-AI cases in tests/fixtures/review_eval_cases.json). The stub
    # reviewer used here makes no governing-law set-membership judgment, so there is
    # no longer a deterministic path that force-fails an unapproved law on reassess;
    # that judgment belongs to the AI and is covered by the real-AI eval gate. The
    # approved-law guard cases below still hold: reassess must never force-fail an
    # approved jurisdiction.

    def test_edited_paragraphs_overlay_applied(self):
        # Extract a paragraph id from a real parse.
        from nda_automation.review_document import split_document_paragraphs
        paragraphs = split_document_paragraphs(SOURCE_TEXT)
        # Find the governing-law paragraph and replace its text.
        gl_paragraph = next(
            (p for p in paragraphs if "England and Wales" in str(p.get("text") or "")),
            None,
        )
        if gl_paragraph is None:
            self.skipTest("Could not locate governing-law paragraph in fixture")
        edited = dict(gl_paragraph)
        edited["text"] = "This Agreement shall be governed by the laws of Delaware."
        result = reassess_single_clause(
            "governing_law",
            SOURCE_TEXT,
            paragraphs=paragraphs,
            edited_paragraphs=[edited],
        )
        self.assertEqual(result["id"], "governing_law")
        # Delaware is an approved law — backstop should NOT force fail.
        self.assertNotEqual(result.get("reason_code"), "unapproved_governing_law")

    def test_unknown_clause_id_raises_reassess_error(self):
        with self.assertRaises(ReassessClauseError) as ctx:
            reassess_single_clause("nonexistent_clause_xyz", SOURCE_TEXT)
        self.assertEqual(ctx.exception.status, 404)

    def test_empty_clause_id_raises_reassess_error(self):
        with self.assertRaises(ReassessClauseError):
            reassess_single_clause("", SOURCE_TEXT)

    def test_reassess_metadata_has_expected_keys(self):
        result = reassess_single_clause("term_and_survival", SOURCE_TEXT)
        meta = result["reassess_metadata"]
        self.assertIn("clause_id", meta)
        self.assertIn("feature", meta)
        self.assertIn("has_edited_paragraphs", meta)
        self.assertIn("ai_verifier_ran", meta)
        self.assertEqual(meta["feature"], "review")


# ------------------------------------------------------------------ #
# HTTP route tests (owner-scoping + cross-tenant denial)
# ------------------------------------------------------------------ #

class ReassessClauseRouteTests(unittest.TestCase):

    def setUp(self):
        telemetry.reset()
        self.repository = InMemoryMatterRepository()
        self.matter = _seed_matter(self.repository, owner_user_id="alice@example.com")
        self.matter_id = self.matter["id"]

    def _handler(self, *, user_id: str, body: dict | None = None) -> _FakeHandler:
        return _FakeHandler(
            current_user_id=user_id,
            body=body,
            repository=self.repository,
        )

    # ---- happy-path -------------------------------------------------

    def test_owner_can_reassess_own_matter_clause(self):
        handler = self._handler(
            user_id="alice@example.com",
            body={"matter_id": self.matter_id, "clause_id": "mutuality"},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 200)
        payload = handler.json
        self.assertIn("clause", payload)
        self.assertEqual(payload["clause"]["id"], "mutuality")
        self.assertEqual(payload["matter_id"], self.matter_id)
        self.assertEqual(payload["clause_id"], "mutuality")
        self.assertIn("reassess_metadata", payload)

    def test_reassess_clause_returns_updated_verdict(self):
        handler = self._handler(
            user_id="alice@example.com",
            body={"matter_id": self.matter_id, "clause_id": "governing_law"},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 200)
        clause = handler.json["clause"]
        self.assertEqual(clause["id"], "governing_law")
        self.assertIn("decision", clause)
        self.assertIn("review_state", clause)

    def test_telemetry_incremented_on_success(self):
        handler = self._handler(
            user_id="alice@example.com",
            body={"matter_id": self.matter_id, "clause_id": "mutuality"},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 200)
        counters = telemetry.snapshot()["counters"]
        self.assertEqual(counters.get("reassess_clause_requests", 0), 1)
        self.assertEqual(counters.get("reassess_clause_completed", 0), 1)

    # ---- cross-tenant denial ----------------------------------------

    def test_cross_tenant_attacker_gets_404_for_another_users_matter(self):
        """Bob cannot reassess Alice's matter — returns 404 (not 403) so matter
        existence is not leaked."""
        handler = self._handler(
            user_id="bob@example.com",
            body={"matter_id": self.matter_id, "clause_id": "mutuality"},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 404)

    def test_no_auth_user_can_access_matter_in_single_tenant_mode(self):
        """An empty owner_user_id is the no-auth / single-tenant path.
        The HTTP auth layer (not the route handler) enforces authentication;
        the route handler scopes by owner only and treats empty as 'all matters
        in scope' (local / no-auth deployments).
        """
        handler = self._handler(
            user_id="",
            body={"matter_id": self.matter_id, "clause_id": "mutuality"},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 200)

    # ---- input validation -------------------------------------------

    def test_missing_matter_id_returns_400(self):
        handler = self._handler(
            user_id="alice@example.com",
            body={"clause_id": "mutuality"},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 400)
        self.assertIn("matter_id", handler.json.get("error", ""))

    def test_missing_clause_id_returns_400(self):
        handler = self._handler(
            user_id="alice@example.com",
            body={"matter_id": self.matter_id},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 400)
        self.assertIn("clause_id", handler.json.get("error", ""))

    def test_unknown_matter_id_returns_404(self):
        handler = self._handler(
            user_id="alice@example.com",
            body={"matter_id": "matter_doesnotexist", "clause_id": "mutuality"},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 404)

    def test_unknown_clause_id_returns_404(self):
        handler = self._handler(
            user_id="alice@example.com",
            body={"matter_id": self.matter_id, "clause_id": "nonexistent_clause_xyz"},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 404)

    def test_empty_payload_returns_400(self):
        handler = self._handler(
            user_id="alice@example.com",
            body={},
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 400)

    def test_edited_text_is_accepted(self):
        edited_text = SOURCE_TEXT.replace(
            "laws of England and Wales", "laws of Delaware"
        )
        handler = self._handler(
            user_id="alice@example.com",
            body={
                "matter_id": self.matter_id,
                "clause_id": "governing_law",
                "edited_text": edited_text,
            },
        )
        handle_reassess_clause(handler)
        self.assertEqual(handler.status, 200)
        clause = handler.json["clause"]
        self.assertEqual(clause["id"], "governing_law")

    def test_two_users_each_see_only_their_own_matter(self):
        bob_matter = _seed_matter(self.repository, owner_user_id="bob@example.com")
        bob_id = bob_matter["id"]

        # Alice can access her matter.
        alice_handler = self._handler(
            user_id="alice@example.com",
            body={"matter_id": self.matter_id, "clause_id": "mutuality"},
        )
        handle_reassess_clause(alice_handler)
        self.assertEqual(alice_handler.status, 200)

        # Bob can access his matter.
        bob_handler = self._handler(
            user_id="bob@example.com",
            body={"matter_id": bob_id, "clause_id": "mutuality"},
        )
        handle_reassess_clause(bob_handler)
        self.assertEqual(bob_handler.status, 200)

        # Alice cannot access Bob's matter.
        alice_bob_handler = self._handler(
            user_id="alice@example.com",
            body={"matter_id": bob_id, "clause_id": "mutuality"},
        )
        handle_reassess_clause(alice_bob_handler)
        self.assertEqual(alice_bob_handler.status, 404)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
