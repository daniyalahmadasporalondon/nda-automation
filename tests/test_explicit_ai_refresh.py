"""No automatic AI on ordinary UI actions -- explicit Refresh-with-AI.

Contract under test (backend half):
  * Opening/fetching a matter's review (GET /api/matters/<id>/review, driven by
    ``handle_matter_review``) returns the EXISTING stored review and a cheap,
    OFFLINE ``review_may_be_stale`` boolean. It must NEVER invoke the AI review
    engine / verifier.
  * The explicit POST /api/matters/<id>/review-refresh
    (``handle_matter_review_refresh``) is the ONLY path that runs the AI review
    engine, and it runs it whenever the broad offline staleness signal is set
    (playbook/engine drift OR no AI review exists OR the matter text changed).

The "AI engine" is represented here by a sentinel ``review_nda_with_active_engine``
replacement that records every call -- so a single assertion proves whether the
expensive path ran.
"""
from __future__ import annotations

import unittest

from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.routes import matters as matters_routes


# --- a stored review that is NOT stale on the narrow playbook/engine axis -------
# Mirrors review_staleness.review_result_stale_reasons: a review is narrowly
# fresh only when its engine version + clauses + review_state + playbook_runtime
# all line up with the active runtime. We stamp the runtime to match whatever the
# active playbook reports so the ONLY thing that can flip review_may_be_stale in a
# given test is the signal that test is exercising.
from nda_automation.checker import REVIEW_ENGINE_VERSION
from nda_automation.playbook_runtime import ensure_active_playbook_runtime


def _active_runtime() -> dict:
    return ensure_active_playbook_runtime()


def _fresh_ai_review_result(extracted_text: str) -> dict:
    runtime = _active_runtime()
    return {
        "review_engine_version": REVIEW_ENGINE_VERSION,
        "extracted_text": extracted_text,
        "clauses": [
            {
                "id": "confidentiality",
                "structure_context": {},
                "review_state": {"state": "pass"},
            }
        ],
        "review_state": {"state": "pass"},
        "playbook_runtime": {
            "active_version_id": runtime["active_version_id"],
            "active_hash": runtime["active_hash"],
            "playbook_name": runtime["playbook_name"],
            "playbook_version": runtime["playbook_version"],
            "published_at": runtime["published_at"],
            "published_by": runtime["published_by"],
        },
        # Marks this as an AI-first review (so "no_ai_review" is NOT a reason).
        "active_review_engine": {"executed_engine": "ai_first", "engine": "ai_first"},
        "ai_first_review": {"status": "completed"},
    }


class _FakeHandler:
    def __init__(self, *, repository, current_user_id: str = "owner@example.com"):
        self.matter_repository = repository
        self.current_user_id = current_user_id
        self.current_user = {"id": current_user_id, "email": current_user_id}
        self.status = None
        self.json = None

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.json = payload


class _SpyEngine:
    """Stands in for review_nda_with_active_engine (the AI path). Records calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text, *, paragraphs=None, **kwargs):
        self.calls += 1
        # Return a fresh AI review so the refresh persists cleanly.
        return _fresh_ai_review_result(text)


def _seed_matter(repository, *, extracted_text: str, review_result: dict, owner: str = "owner@example.com") -> str:
    matter = repository.create_matter(
        source_filename="nda.txt",
        document_bytes=b"",
        extracted_text=extracted_text,
        review_result=review_result,
        triage={},
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id=owner,
    )
    return str(matter["id"])


class OpenMatterDoesNotRunAI(unittest.TestCase):
    def test_fetching_review_returns_stored_review_without_calling_ai(self):
        repository = InMemoryMatterRepository()
        text = "Confidential Information means non-public business information."
        matter_id = _seed_matter(
            repository,
            extracted_text=text,
            review_result=_fresh_ai_review_result(text),
        )

        spy = _SpyEngine()
        # Patch the engine that the refresh path would use; the open path must not
        # touch it. If handle_matter_review ever calls the AI engine, spy.calls > 0.
        original = matters_routes.review_nda_with_active_engine
        matters_routes.review_nda_with_active_engine = spy
        try:
            handler = _FakeHandler(repository=repository)
            matters_routes.handle_matter_review(handler, f"/api/matters/{matter_id}/review")
        finally:
            matters_routes.review_nda_with_active_engine = original

        self.assertEqual(handler.status, 200)
        self.assertEqual(spy.calls, 0, "opening a matter must NOT invoke the AI review engine")
        # The existing stored review is returned as-is.
        self.assertIn("review_result", handler.json)
        # And the cheap offline staleness signal is present + False for a fresh AI review.
        self.assertIn("review_may_be_stale", handler.json)
        self.assertFalse(handler.json["review_may_be_stale"])
        self.assertFalse(handler.json["review_refresh"]["review_may_be_stale"])

    def test_review_may_be_stale_true_when_no_ai_review_exists(self):
        repository = InMemoryMatterRepository()
        text = "Some NDA text without an AI review."
        # A deterministically-reviewed matter: a valid review_result but the engine
        # marker is NOT ai_first -> "no AI review exists" -> may_be_stale True.
        deterministic_review = _fresh_ai_review_result(text)
        deterministic_review["active_review_engine"] = {"executed_engine": "deterministic", "engine": "deterministic"}
        deterministic_review.pop("ai_first_review", None)
        matter_id = _seed_matter(repository, extracted_text=text, review_result=deterministic_review)

        handler = _FakeHandler(repository=repository)
        matters_routes.handle_matter_review(handler, f"/api/matters/{matter_id}/review")

        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.json["review_may_be_stale"])
        self.assertIn("no_ai_review", handler.json["review_refresh"]["stale_reasons"])

    def test_review_may_be_stale_true_when_matter_text_changed(self):
        repository = InMemoryMatterRepository()
        # The stored AI review ran on the ORIGINAL text; the matter's current text
        # has since changed (the review_result records the snapshot it ran on).
        review_result = _fresh_ai_review_result("Original confidential information clause.")
        matter_id = _seed_matter(
            repository,
            extracted_text="Edited confidential information clause -- materially different.",
            review_result=review_result,
        )

        handler = _FakeHandler(repository=repository)
        matters_routes.handle_matter_review(handler, f"/api/matters/{matter_id}/review")

        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.json["review_may_be_stale"])
        self.assertIn("matter_text_changed", handler.json["review_refresh"]["stale_reasons"])


class ExplicitRefreshRunsAI(unittest.TestCase):
    def test_review_refresh_endpoint_runs_the_ai_engine(self):
        repository = InMemoryMatterRepository()
        text = "Confidential information clause that needs an AI review."
        # No AI review yet -> broad staleness True -> the explicit refresh must run AI.
        deterministic_review = _fresh_ai_review_result(text)
        deterministic_review["active_review_engine"] = {"executed_engine": "deterministic"}
        deterministic_review.pop("ai_first_review", None)
        matter_id = _seed_matter(repository, extracted_text=text, review_result=deterministic_review)

        spy = _SpyEngine()
        original = matters_routes.review_nda_with_active_engine
        matters_routes.review_nda_with_active_engine = spy
        try:
            handler = _FakeHandler(repository=repository)
            matters_routes.handle_matter_review_refresh(
                handler, f"/api/matters/{matter_id}/review-refresh"
            )
        finally:
            matters_routes.review_nda_with_active_engine = original

        self.assertEqual(handler.status, 200)
        self.assertEqual(spy.calls, 1, "the explicit review-refresh endpoint MUST run the AI engine")
        # After the AI refresh the stored review is the AI one -> no longer may-be-stale.
        self.assertFalse(handler.json["review_may_be_stale"])

    def test_review_refresh_runs_ai_even_when_only_text_changed(self):
        repository = InMemoryMatterRepository()
        # Stored AI review ran on the OLD text; the matter text has since changed.
        review_result = _fresh_ai_review_result("Original clause text.")
        matter_id = _seed_matter(
            repository,
            extracted_text="Brand new clause text after an edit.",
            review_result=review_result,
        )

        spy = _SpyEngine()
        original = matters_routes.review_nda_with_active_engine
        matters_routes.review_nda_with_active_engine = spy
        try:
            handler = _FakeHandler(repository=repository)
            matters_routes.handle_matter_review_refresh(
                handler, f"/api/matters/{matter_id}/review-refresh"
            )
        finally:
            matters_routes.review_nda_with_active_engine = original

        self.assertEqual(handler.status, 200)
        self.assertEqual(spy.calls, 1, "explicit refresh must run AI on a text-changed matter")


if __name__ == "__main__":
    unittest.main()
