"""Async AI review backend: POST /api/matters/<id>/review-refresh is NON-BLOCKING.

The explicit Review-tab refresh used to run the heavy AI pipeline SYNCHRONOUSLY
inside the request (~145-245s -> broken pipe). It now ENQUEUES onto the SAME
storm-hardened inbound worker pool and returns immediately. These tests pin the
202 contract + the async status lifecycle + the storm-safety invariants:

  * the route returns immediately and NEVER runs the heavy engine inline,
  * dedup (a 2nd enqueue while one is pending does NOT run a 2nd review),
  * the bounded queue full -> 503,
  * the status lifecycle idle -> in_progress -> completed,
  * a failed review -> failed + review_error,
  * the in_progress TTL staleness override (the restart guard) reads as the
    DISTINCT ``stalled`` status (NOT failed) WITHOUT mutating storage on a GET,
    so a pure timeout never fabricates a durable failure,
  * idempotency: an already-AI-reviewed matter is a no-op in the worker.

The pool itself (concurrency bound, dedup set, queue cap, recovery sweep, worker
loop) is exercised by tests/test_inbound_auto_review.py and is UNCHANGED here --
the on-demand path only REUSES it.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from nda_automation import ingestion_service, matter_view
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.review_engine import ActiveReviewEngineError
from nda_automation.routes import matters as matters_routes

from nda_automation.checker import REVIEW_ENGINE_VERSION
from nda_automation.playbook_runtime import ensure_active_playbook_runtime


# --------------------------------------------------------------------------- #
# Fixtures (mirror tests/test_explicit_ai_refresh.py + test_inbound_auto_review)
# --------------------------------------------------------------------------- #
def _active_runtime() -> dict:
    return ensure_active_playbook_runtime()


def _fresh_ai_review_result(extracted_text: str) -> dict:
    runtime = _active_runtime()
    return {
        "review_engine_version": REVIEW_ENGINE_VERSION,
        "extracted_text": extracted_text,
        "clauses": [
            {"id": "confidentiality", "structure_context": {}, "review_state": {"state": "pass"}}
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
        "active_review_engine": {"executed_engine": "ai_first", "engine": "ai_first"},
        "ai_first_review": {"status": "completed"},
    }


def _stale_deterministic_review(extracted_text: str) -> dict:
    """A stored review that triggers BROAD staleness (no AI review exists)."""
    review = _fresh_ai_review_result(extracted_text)
    review["active_review_engine"] = {"executed_engine": "deterministic"}
    review.pop("ai_first_review", None)
    return review


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


class _FreshPoolMixin:
    """Swap the module pool for a fresh, isolated one per test (never touch live)."""

    def _fresh_pool(self):
        pool = ingestion_service._InboundReviewWorkerPool()
        self._orig_pool = ingestion_service._INBOUND_REVIEW_POOL
        ingestion_service._INBOUND_REVIEW_POOL = pool
        self.addCleanup(self._restore_pool)
        # Clear the on-demand registry between tests.
        with ingestion_service._ON_DEMAND_REVIEW_LOCK:
            ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()
        return pool

    def _restore_pool(self):
        ingestion_service._INBOUND_REVIEW_POOL = self._orig_pool
        with ingestion_service._ON_DEMAND_REVIEW_LOCK:
            ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()

    def _force_ai_enabled(self):
        orig = matters_routes._ai_first_review_enabled
        matters_routes._ai_first_review_enabled = lambda: True
        self.addCleanup(lambda: setattr(matters_routes, "_ai_first_review_enabled", orig))


# --------------------------------------------------------------------------- #
# 1. The route returns 202 IMMEDIATELY and NEVER runs the heavy engine inline.
# --------------------------------------------------------------------------- #
class RouteIsNonBlocking(_FreshPoolMixin, unittest.TestCase):
    def test_stale_matter_returns_202_and_does_not_run_engine_inline(self):
        pool = self._fresh_pool()
        self._force_ai_enabled()
        # Configure the pool with a handler that records calls but does NOT run --
        # so we can prove the route did not block on (or inline) the engine.
        ran: list[str] = []
        pool.configure(lambda mid, owner: ran.append(mid))

        repository = InMemoryMatterRepository()
        text = "Confidential information clause needing an AI review."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))

        # The engine spy: if the ROUTE ever ran the engine inline, this fires.
        engine_calls: list[str] = []
        orig_engine = matters_routes.review_nda_with_active_engine
        matters_routes.review_nda_with_active_engine = lambda *a, **k: engine_calls.append("x")
        try:
            handler = _FakeHandler(repository=repository)
            matters_routes.handle_matter_review_refresh(handler, f"/api/matters/{matter_id}/review-refresh")
        finally:
            matters_routes.review_nda_with_active_engine = orig_engine

        self.assertEqual(handler.status, 202)
        self.assertEqual(handler.json["review_status"], "in_progress")
        self.assertTrue(handler.json["job_scheduled"])
        self.assertEqual(engine_calls, [], "the route must NOT run the AI engine inline")
        # The enqueue stamped in_progress on the stored matter.
        stored = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        self.assertEqual(stored["review_status"], "in_progress")

    def test_not_stale_matter_returns_200_idle_and_enqueues_nothing(self):
        pool = self._fresh_pool()
        self._force_ai_enabled()
        ran: list[str] = []
        pool.configure(lambda mid, owner: ran.append(mid))

        repository = InMemoryMatterRepository()
        text = "Confidential information clause with a fresh AI review."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_fresh_ai_review_result(text))

        handler = _FakeHandler(repository=repository)
        matters_routes.handle_matter_review_refresh(handler, f"/api/matters/{matter_id}/review-refresh")

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.json["review_status"], "idle")
        self.assertFalse(handler.json["job_scheduled"])
        self.assertEqual(pool.pending_count(), 0, "a fresh matter must enqueue nothing")

    def test_missing_matter_returns_404(self):
        self._fresh_pool()
        self._force_ai_enabled()
        repository = InMemoryMatterRepository()
        handler = _FakeHandler(repository=repository)
        matters_routes.handle_matter_review_refresh(handler, "/api/matters/does-not-exist/review-refresh")
        self.assertEqual(handler.status, 404)

    def test_ai_unavailable_keeps_synchronous_notification_and_enqueues_nothing(self):
        pool = self._fresh_pool()
        # AI reviewer OFF.
        orig = matters_routes._ai_first_review_enabled
        matters_routes._ai_first_review_enabled = lambda: False
        self.addCleanup(lambda: setattr(matters_routes, "_ai_first_review_enabled", orig))
        ran: list[str] = []
        pool.configure(lambda mid, owner: ran.append(mid))

        repository = InMemoryMatterRepository()
        text = "Confidential information clause while AI is OFF."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))

        handler = _FakeHandler(repository=repository)
        matters_routes.handle_matter_review_refresh(handler, f"/api/matters/{matter_id}/review-refresh")

        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.json["ai_review_unavailable"])
        self.assertIn("no AI reviewer available", handler.json["ai_review_unavailable_message"])
        self.assertEqual(pool.pending_count(), 0, "AI-off must NOT enqueue a doomed job")


# --------------------------------------------------------------------------- #
# 2. Dedup + queue-full at the enqueue layer.
# --------------------------------------------------------------------------- #
class EnqueueDedupAndQueueFull(_FreshPoolMixin, unittest.TestCase):
    def test_second_enqueue_while_pending_is_a_dedup_no_op(self):
        pool = self._fresh_pool()
        # A handler that blocks so the job stays pending while we re-enqueue.
        import threading

        gate = threading.Event()
        runs: list[str] = []

        def _handler(mid, owner):
            runs.append(mid)
            gate.wait(timeout=5)

        pool.configure(_handler)

        scheduled1, pending1, full1 = ingestion_service.enqueue_on_demand_review("m1", "owner")
        # Give the worker a beat to pick it up so it is pending/in-flight.
        import time

        time.sleep(0.05)
        scheduled2, pending2, full2 = ingestion_service.enqueue_on_demand_review("m1", "owner")

        self.assertEqual((scheduled1, pending1, full1), (True, False, False))
        self.assertEqual((scheduled2, pending2, full2), (False, True, False), "2nd enqueue is dedup'd")

        gate.set()
        pool._join_for_tests(timeout=5)
        self.assertEqual(runs.count("m1"), 1, "the dedup'd matter ran the review only ONCE")

    def test_queue_full_returns_queue_full_flag(self):
        pool = self._fresh_pool()
        # Stub the pool so enqueue always reports "full" (queue.Full path).
        original_enqueue = pool.enqueue
        pool.enqueue = lambda mid, owner: False  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(pool, "enqueue", original_enqueue))

        scheduled, pending, full = ingestion_service.enqueue_on_demand_review("m2", "owner")
        self.assertEqual((scheduled, pending, full), (False, False, True))

    def test_route_queue_full_returns_503_idle(self):
        pool = self._fresh_pool()
        self._force_ai_enabled()
        pool.enqueue = lambda mid, owner: False  # type: ignore[assignment]

        repository = InMemoryMatterRepository()
        text = "Confidential clause that should enqueue but the queue is full."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))

        handler = _FakeHandler(repository=repository)
        matters_routes.handle_matter_review_refresh(handler, f"/api/matters/{matter_id}/review-refresh")

        self.assertEqual(handler.status, 503)
        self.assertEqual(handler.json["review_status"], "idle")
        self.assertFalse(handler.json["job_scheduled"])


# --------------------------------------------------------------------------- #
# 3. Status lifecycle idle -> in_progress -> completed (worker run).
# --------------------------------------------------------------------------- #
class StatusLifecycle(_FreshPoolMixin, unittest.TestCase):
    def test_completed_status_stamped_after_a_successful_async_review(self):
        repository = InMemoryMatterRepository()
        text = "Confidential information clause to be AI reviewed."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))

        # Stamp in_progress (what the enqueue does).
        repository.update_matter_fields(
            matter_id,
            {"review_status": "in_progress", "review_started_at": datetime.now(timezone.utc).isoformat()},
            owner_user_id="owner@example.com",
        )

        ai_review = _fresh_ai_review_result(text)

        def _engine(t, *, paragraphs=None, **kwargs):
            return ai_review

        # Run the review body directly against the in-memory repo (no pool).
        ingestion_service._perform_inbound_ai_review(
            matter_id,
            repository=repository,
            owner_user_id="owner@example.com",
            review_engine_func=_engine,
        )

        stored = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        self.assertEqual(stored["review_status"], "completed")
        self.assertEqual(stored.get("review_error", ""), "")
        # The AI review actually landed.
        self.assertEqual(stored["review_result"]["active_review_engine"]["executed_engine"], "ai_first")

    def test_failed_review_stamps_failed_and_review_error(self):
        repository = InMemoryMatterRepository()
        text = "Confidential clause where the AI reviewer is unavailable."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))

        def _unavailable(t, *, paragraphs=None, **kwargs):
            raise ActiveReviewEngineError("AI-first review failed: disabled")

        ingestion_service._perform_inbound_ai_review(
            matter_id,
            repository=repository,
            owner_user_id="owner@example.com",
            review_engine_func=_unavailable,
        )

        stored = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        self.assertEqual(stored["review_status"], "failed")
        self.assertTrue(stored.get("review_error"))
        # The stored review is UNTOUCHED (no deterministic verdict written).
        self.assertEqual(stored["review_result"]["active_review_engine"]["executed_engine"], "deterministic")

    def test_idempotency_already_ai_reviewed_is_a_no_op(self):
        repository = InMemoryMatterRepository()
        text = "Confidential clause already AI reviewed."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_fresh_ai_review_result(text))

        engine_calls: list[str] = []

        def _engine(t, *, paragraphs=None, **kwargs):
            engine_calls.append(t)
            return _fresh_ai_review_result(t)

        ingestion_service._perform_inbound_ai_review(
            matter_id,
            repository=repository,
            owner_user_id="owner@example.com",
            review_engine_func=_engine,
        )

        self.assertEqual(engine_calls, [], "an already-ai_first matter must NOT be re-reviewed")


# --------------------------------------------------------------------------- #
# 4. The in_progress TTL staleness override (the restart guard).
#
# A stale in_progress is reported as the DISTINCT ``stalled`` status -- NOT ``failed``
# -- so a slow/interrupted (but not durably-errored) review never paints a red
# failure. Only the durable failure path writes ``failed``.
# --------------------------------------------------------------------------- #
class InProgressTTLOverride(_FreshPoolMixin, unittest.TestCase):
    def test_stored_in_progress_past_ttl_reads_as_stalled_not_failed_without_mutating(self):
        repository = InMemoryMatterRepository()
        text = "Confidential clause whose review thread died mid-run."
        # An in_progress stamp older than the TTL (a crashed/restarted/long worker).
        stale_started = (
            datetime.now(timezone.utc) - timedelta(seconds=matter_view.REVIEW_IN_PROGRESS_TTL_SECONDS + 60)
        ).isoformat()
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))
        repository.update_matter_fields(
            matter_id,
            {"review_status": "in_progress", "review_started_at": stale_started},
            owner_user_id="owner@example.com",
        )

        # Read via the review view (the poll path): the override fires as STALLED, not
        # failed -- a pure timeout must never be reported as a durable failure.
        stored = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        view = matter_view.review_matter(stored)
        self.assertEqual(view["review_status"], matter_view.REVIEW_STATUS_STALLED)
        self.assertNotEqual(view["review_status"], "failed")
        # The message is an honest "taking longer / interrupted, retry", not an error.
        self.assertRegex(view["review_error"].lower(), r"longer|interrupt")

        # And it did NOT mutate storage (the override is read-only).
        reread = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        self.assertEqual(reread["review_status"], "in_progress", "the GET must not have mutated stored status")

    def test_recent_in_progress_still_reads_as_in_progress(self):
        repository = InMemoryMatterRepository()
        text = "Confidential clause whose review is genuinely running."
        recent = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))
        repository.update_matter_fields(
            matter_id,
            {"review_status": "in_progress", "review_started_at": recent},
            owner_user_id="owner@example.com",
        )

        stored = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        view = matter_view.review_matter(stored)
        self.assertEqual(view["review_status"], "in_progress")

    def test_board_view_also_applies_the_ttl_override(self):
        repository = InMemoryMatterRepository()
        text = "Board card whose review thread died."
        stale_started = (
            datetime.now(timezone.utc) - timedelta(seconds=matter_view.REVIEW_IN_PROGRESS_TTL_SECONDS + 60)
        ).isoformat()
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))
        repository.update_matter_fields(
            matter_id,
            {"review_status": "in_progress", "review_started_at": stale_started},
            owner_user_id="owner@example.com",
        )

        stored = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        public = matter_view.public_matter(stored)
        # The board view also reports STALLED (not failed) for a stale in_progress.
        self.assertEqual(public["review_status"], matter_view.REVIEW_STATUS_STALLED)
        self.assertNotEqual(public["review_status"], "failed")

    def test_durable_failure_still_reads_as_failed_distinct_from_stalled(self):
        # The OTHER channel: a genuine, durably-recorded failure (the only thing that
        # writes review_status="failed") must STILL read as "failed" through the same
        # view path that emits "stalled". This proves the two are distinct -- a real
        # error keeps its red-failure channel; only a pure timeout becomes "stalled".
        repository = InMemoryMatterRepository()
        text = "Confidential clause where the AI reviewer is unavailable."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))

        def _unavailable(t, *, paragraphs=None, **kwargs):
            raise ActiveReviewEngineError("AI-first review failed: disabled")

        ingestion_service._perform_inbound_ai_review(
            matter_id,
            repository=repository,
            owner_user_id="owner@example.com",
            review_engine_func=_unavailable,
        )

        stored = repository.get_matter(matter_id, owner_user_id="owner@example.com")
        view = matter_view.review_matter(stored)
        self.assertEqual(view["review_status"], "failed")
        self.assertNotEqual(view["review_status"], matter_view.REVIEW_STATUS_STALLED)
        self.assertTrue(view.get("review_error"))


# --------------------------------------------------------------------------- #
# 5. The on-demand path runs the AI-ONLY engine (fail-closed AI-first).
# --------------------------------------------------------------------------- #
class OnDemandUsesAIOnlyEngine(_FreshPoolMixin, unittest.TestCase):
    def test_pool_handler_pins_ai_first_for_on_demand_jobs(self):
        pool = self._fresh_pool()
        # Use the REAL handler so the engine-selection branch is exercised.
        pool.configure(ingestion_service._inbound_review_pool_handler)

        captured_force: list[object] = []
        orig_engine = ingestion_service.review_nda_with_active_engine

        def _recording_engine(text, *, paragraphs=None, **kwargs):
            captured_force.append(kwargs.get("force_engine"))
            return _fresh_ai_review_result(text)

        ingestion_service.review_nda_with_active_engine = _recording_engine
        self.addCleanup(lambda: setattr(ingestion_service, "review_nda_with_active_engine", orig_engine))

        repository = InMemoryMatterRepository()
        text = "Confidential clause for the on-demand AI-only review."
        matter_id = _seed_matter(repository, extracted_text=text, review_result=_stale_deterministic_review(text))

        # Mark on-demand + enqueue using the in-memory repo for the stamp; the worker
        # re-resolves the disk repo, so drive the handler engine selection directly.
        with ingestion_service._ON_DEMAND_REVIEW_LOCK:
            ingestion_service._ON_DEMAND_REVIEW_MATTERS.add((matter_id, "owner@example.com"))
        # Patch DiskMatterRepository so the worker uses our in-memory repo.
        orig_disk = ingestion_service.DiskMatterRepository
        ingestion_service.DiskMatterRepository = lambda: repository
        self.addCleanup(lambda: setattr(ingestion_service, "DiskMatterRepository", orig_disk))

        ingestion_service._inbound_review_pool_handler(matter_id, "owner@example.com")

        self.assertEqual(captured_force, ["ai_first"], "on-demand jobs must force the AI-first engine")
        # And the marker is cleared after a terminal run.
        self.assertFalse(ingestion_service._is_on_demand_review(matter_id, "owner@example.com"))


if __name__ == "__main__":
    unittest.main()
