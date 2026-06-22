"""No automatic AI on ordinary UI actions -- explicit Refresh-with-AI (ASYNC).

Contract under test (backend half):
  * Opening/fetching a matter's review (GET /api/matters/<id>/review, driven by
    ``handle_matter_review``) returns the EXISTING stored review and a cheap,
    OFFLINE ``review_may_be_stale`` boolean. It must NEVER invoke the AI review
    engine / verifier.
  * The explicit POST /api/matters/<id>/review-refresh
    (``handle_matter_review_refresh``) is the user-initiated AI refresh. It is now
    ASYNC: it ENQUEUES the AI review onto the storm-hardened inbound worker pool and
    returns 202 immediately -- it must NEVER run the heavy engine inline. AI-OFF is
    decided by a cheap offline check and keeps today's synchronous
    ``ai_review_unavailable`` notification (no doomed job is enqueued).

The detailed 202 contract / status lifecycle / TTL override live in
tests/test_async_review_backend.py; this file pins the no-AI-on-open invariant and
the async never-run-inline invariant.
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
    def __init__(self, *, repository, current_user_id: str = "owner@example.com", body=None):
        self.matter_repository = repository
        self.current_user_id = current_user_id
        self.current_user = {"id": current_user_id, "email": current_user_id}
        self.status = None
        self.json = None
        # The parsed JSON request body the route reads via _read_json_payload. None
        # ==> behave like a legacy no-body caller (the route's getattr guard / empty
        # default), matching POSTs with no JSON payload.
        self._body = body

    def _read_json_payload(self):
        # Mirror server._read_json_payload's contract: an absent body parses to {}.
        return {} if self._body is None else dict(self._body)

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

class ExplicitRefreshEnqueuesAsync(unittest.TestCase):
    """The explicit refresh ENQUEUES the AI review async (202) and NEVER runs it
    inline. The pool is swapped for a fresh, isolated instance per test so we never
    touch the live module pool, and configured with a NON-running handler so a 202
    can be asserted without any heavy work."""

    def setUp(self):
        from nda_automation import ingestion_service

        self._ingestion = ingestion_service
        self._orig_pool = ingestion_service._INBOUND_REVIEW_POOL
        self._pool = ingestion_service._InboundReviewWorkerPool()
        self._pool.configure(lambda mid, owner: None)  # never actually run
        ingestion_service._INBOUND_REVIEW_POOL = self._pool
        with ingestion_service._ON_DEMAND_REVIEW_LOCK:
            ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()
        self._orig_ai_enabled = matters_routes._ai_first_review_enabled
        matters_routes._ai_first_review_enabled = lambda: True

    def tearDown(self):
        self._ingestion._INBOUND_REVIEW_POOL = self._orig_pool
        with self._ingestion._ON_DEMAND_REVIEW_LOCK:
            self._ingestion._ON_DEMAND_REVIEW_MATTERS.clear()
        matters_routes._ai_first_review_enabled = self._orig_ai_enabled

    def test_review_refresh_endpoint_enqueues_and_never_runs_engine_inline(self):
        repository = InMemoryMatterRepository()
        text = "Confidential information clause that needs an AI review."
        # No AI review yet -> broad staleness True -> the explicit refresh enqueues.
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

        self.assertEqual(handler.status, 202)
        self.assertEqual(spy.calls, 0, "the route MUST NOT run the AI engine inline")
        self.assertEqual(handler.json["review_status"], "in_progress")
        self.assertTrue(handler.json["job_scheduled"])

    def test_review_refresh_enqueues_when_only_text_changed(self):
        repository = InMemoryMatterRepository()
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

        self.assertEqual(handler.status, 202)
        self.assertEqual(spy.calls, 0, "text-changed refresh enqueues, never runs inline")
        self.assertEqual(handler.json["review_status"], "in_progress")

    def test_fresh_ai_review_returns_200_idle_and_enqueues_nothing(self):
        repository = InMemoryMatterRepository()
        text = "Confidential clause that already has a fresh AI review."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_fresh_ai_review_result(text))

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
        self.assertEqual(spy.calls, 0)
        self.assertEqual(handler.json["review_status"], "idle")
        self.assertFalse(handler.json["job_scheduled"])

    def test_force_re_reviews_a_fresh_not_stale_matter(self):
        # An already-reviewed, NOT-stale matter (the Review button shows "Reviewed").
        # Without force this returns 200 idle (above); with force:true (the Review
        # click) it must RE-ENQUEUE the AI review (202 in_progress) so the operator
        # can re-run on demand. The engine is still never run inline.
        repository = InMemoryMatterRepository()
        text = "Confidential clause that already has a fresh AI review."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_fresh_ai_review_result(text))

        spy = _SpyEngine()
        original = matters_routes.review_nda_with_active_engine
        matters_routes.review_nda_with_active_engine = spy
        try:
            handler = _FakeHandler(repository=repository, body={"force": True})
            matters_routes.handle_matter_review_refresh(
                handler, f"/api/matters/{matter_id}/review-refresh"
            )
        finally:
            matters_routes.review_nda_with_active_engine = original

        self.assertEqual(handler.status, 202)
        self.assertEqual(spy.calls, 0, "the forced re-run MUST NOT run the AI engine inline")
        self.assertEqual(handler.json["review_status"], "in_progress")
        self.assertTrue(handler.json["job_scheduled"])

    def test_force_false_still_idles_on_a_fresh_matter(self):
        # An explicit force:false body must behave exactly like no body: a fresh,
        # not-stale matter stays idle (the gate is preserved).
        repository = InMemoryMatterRepository()
        text = "Confidential clause that already has a fresh AI review."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_fresh_ai_review_result(text))

        spy = _SpyEngine()
        original = matters_routes.review_nda_with_active_engine
        matters_routes.review_nda_with_active_engine = spy
        try:
            handler = _FakeHandler(repository=repository, body={"force": False})
            matters_routes.handle_matter_review_refresh(
                handler, f"/api/matters/{matter_id}/review-refresh"
            )
        finally:
            matters_routes.review_nda_with_active_engine = original

        self.assertEqual(handler.status, 200)
        self.assertEqual(spy.calls, 0)
        self.assertEqual(handler.json["review_status"], "idle")
        self.assertFalse(handler.json["job_scheduled"])


class ReviewTabIsAIOnly(unittest.TestCase):
    """The user-facing Review tab uses the AI as the ONLY reviewer.

    AI-OFF is decided by the cheap offline ``_ai_first_review_enabled`` predicate
    (NOT by running the engine): the route keeps today's synchronous
    ``ai_review_unavailable`` notification and enqueues NO doomed job. AI-ON enqueues
    the async review (202) and never produces a deterministic verdict.
    """

    def setUp(self):
        from nda_automation import ingestion_service

        self._ingestion = ingestion_service
        self._orig_pool = ingestion_service._INBOUND_REVIEW_POOL
        self._pool = ingestion_service._InboundReviewWorkerPool()
        self._pool.configure(lambda mid, owner: None)
        ingestion_service._INBOUND_REVIEW_POOL = self._pool
        with ingestion_service._ON_DEMAND_REVIEW_LOCK:
            ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()
        self._orig_ai_enabled = matters_routes._ai_first_review_enabled

    def tearDown(self):
        self._ingestion._INBOUND_REVIEW_POOL = self._orig_pool
        with self._ingestion._ON_DEMAND_REVIEW_LOCK:
            self._ingestion._ON_DEMAND_REVIEW_MATTERS.clear()
        matters_routes._ai_first_review_enabled = self._orig_ai_enabled

    def test_ai_off_keeps_synchronous_notification_and_enqueues_nothing(self):
        matters_routes._ai_first_review_enabled = lambda: False
        repository = InMemoryMatterRepository()
        text = "Confidential information clause needing review while AI is OFF."
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
        self.assertEqual(spy.calls, 0, "AI-off must NOT run (or enqueue) the engine")
        self.assertTrue(handler.json["ai_review_unavailable"])
        self.assertIn("no AI reviewer available", handler.json["ai_review_unavailable_message"])
        self.assertEqual(self._pool.pending_count(), 0)
        # The stored review is untouched -- no deterministic verdict produced.
        stored = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        self.assertEqual(
            stored["review_result"]["active_review_engine"]["executed_engine"], "deterministic"
        )

    def test_ai_on_enqueues_async_without_notification(self):
        matters_routes._ai_first_review_enabled = lambda: True
        repository = InMemoryMatterRepository()
        text = "Confidential information clause with the AI reviewer ON."
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

        self.assertEqual(handler.status, 202)
        self.assertEqual(spy.calls, 0, "the route enqueues; it never runs the engine inline")
        self.assertEqual(handler.json["review_status"], "in_progress")
        self.assertNotIn("ai_review_unavailable", handler.json)




if __name__ == "__main__":
    unittest.main()
