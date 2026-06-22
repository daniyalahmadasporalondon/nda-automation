"""Review-pipeline robustness: boot reconcile, pool concurrency, wall-clock cap.

Covers the BACKEND half of the review-recovery hardening:

  * ``reconcile_interrupted_reviews`` heals reviews orphaned by a restart -- a
    stored ``in_progress`` demotes to the durable, RECOVERABLE ``interrupted``
    status (or ``completed`` when the matter already carries a full ai_first
    result), writes durably + owner-safe + idempotently, and -- the HARD
    invariant -- enqueues NOTHING (pure status reconcile, no AI call, no pool).
  * the boot hook (``server._reconcile_interrupted_reviews``) is fail-safe.
  * the worker pool drains TWO jobs concurrently at the bumped default.
  * the per-job wall-clock deadline fires: a body that overruns the budget frees
    the slot and stamps a terminal ``interrupted`` status.
"""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from nda_automation import ingestion_service
from nda_automation.matter_repository import InMemoryMatterRepository


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _seed_matter(
    repo: InMemoryMatterRepository,
    *,
    review_status: str = "in_progress",
    review_started_at: str | None = None,
    ai_first: bool = False,
    owner_user_id: str = "",
) -> dict[str, Any]:
    """Create a matter and stamp the requested async-review lifecycle state."""

    review_result: dict[str, Any] = {"clauses": []}
    if ai_first:
        review_result = {
            "review_mode": "ai_first",
            "active_review_engine": {"executed_engine": "ai_first"},
            "clauses": [],
        }
    matter = repo.create_matter(
        source_filename="NDA.docx",
        document_bytes=b"PK\x03\x04stub",
        extracted_text="Some NDA text.",
        review_result=review_result,
        triage={},
        owner_user_id=owner_user_id,
    )
    fields: dict[str, Any] = {"review_status": review_status}
    if review_started_at is not None:
        fields["review_started_at"] = review_started_at
    repo.update_matter_fields(matter["id"], fields, owner_user_id=owner_user_id)
    return repo.get_matter(matter["id"], owner_user_id=owner_user_id)


# --------------------------------------------------------------------------- #
# TASK 1 -- boot reconcile of interrupted reviews
# --------------------------------------------------------------------------- #
class TestReconcileInterruptedReviews:
    def test_in_progress_without_result_demotes_to_interrupted(self) -> None:
        repo = InMemoryMatterRepository()
        matter = _seed_matter(repo, review_status="in_progress", review_started_at="2000-01-01T00:00:00+00:00")

        summary = ingestion_service.reconcile_interrupted_reviews(repository=repo)

        healed = repo.get_matter(matter["id"])
        assert healed["review_status"] == "interrupted"
        assert "interrupted by a restart" in healed["review_error"].lower() or "interrupted" in healed["review_error"].lower()
        assert summary["interrupted"] == 1
        assert summary["completed"] == 0

    def test_in_progress_with_ai_first_result_heals_to_completed(self) -> None:
        repo = InMemoryMatterRepository()
        matter = _seed_matter(
            repo,
            review_status="in_progress",
            review_started_at="2000-01-01T00:00:00+00:00",
            ai_first=True,
        )

        summary = ingestion_service.reconcile_interrupted_reviews(repository=repo)

        healed = repo.get_matter(matter["id"])
        assert healed["review_status"] == "completed"
        assert healed["review_error"] == ""
        assert summary["completed"] == 1
        assert summary["interrupted"] == 0

    def test_review_started_at_is_preserved(self) -> None:
        repo = InMemoryMatterRepository()
        started = "2000-01-01T00:00:00+00:00"
        matter = _seed_matter(repo, review_status="in_progress", review_started_at=started)

        ingestion_service.reconcile_interrupted_reviews(repository=repo)

        healed = repo.get_matter(matter["id"])
        assert healed["review_started_at"] == started

    def test_recent_in_progress_within_grace_is_left_untouched(self) -> None:
        repo = InMemoryMatterRepository()
        # A start stamp NOW is within the 60s grace -- a warm-restart worker may
        # still be finishing it, so reconcile must not touch it.
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        matter = _seed_matter(repo, review_status="in_progress", review_started_at=now)

        summary = ingestion_service.reconcile_interrupted_reviews(repository=repo)

        healed = repo.get_matter(matter["id"])
        assert healed["review_status"] == "in_progress"
        assert summary["scanned"] == 0

    def test_terminal_statuses_are_ignored(self) -> None:
        repo = InMemoryMatterRepository()
        completed = _seed_matter(repo, review_status="completed")
        failed = _seed_matter(repo, review_status="failed")
        idle = _seed_matter(repo, review_status="idle")

        summary = ingestion_service.reconcile_interrupted_reviews(repository=repo)

        assert repo.get_matter(completed["id"])["review_status"] == "completed"
        assert repo.get_matter(failed["id"])["review_status"] == "failed"
        assert repo.get_matter(idle["id"])["review_status"] == "idle"
        assert summary == {"scanned": 0, "interrupted": 0, "completed": 0, "errors": 0}

    def test_idempotent_second_run_is_a_noop(self) -> None:
        repo = InMemoryMatterRepository()
        matter = _seed_matter(repo, review_status="in_progress", review_started_at="2000-01-01T00:00:00+00:00")

        ingestion_service.reconcile_interrupted_reviews(repository=repo)
        first = repo.get_matter(matter["id"])
        # Second run: the matter is now "interrupted", no longer "in_progress",
        # so it is not scanned/re-written.
        summary = ingestion_service.reconcile_interrupted_reviews(repository=repo)
        second = repo.get_matter(matter["id"])

        assert summary == {"scanned": 0, "interrupted": 0, "completed": 0, "errors": 0}
        assert second["review_status"] == "interrupted"
        assert second["updated_at"] == first["updated_at"]

    def test_owner_scoped_write_uses_each_matters_owner(self) -> None:
        repo = InMemoryMatterRepository()
        a = _seed_matter(repo, review_status="in_progress", review_started_at="2000-01-01T00:00:00+00:00", owner_user_id="user-a")
        b = _seed_matter(repo, review_status="in_progress", review_started_at="2000-01-01T00:00:00+00:00", owner_user_id="user-b")

        summary = ingestion_service.reconcile_interrupted_reviews(repository=repo)

        assert repo.get_matter(a["id"], owner_user_id="user-a")["review_status"] == "interrupted"
        assert repo.get_matter(b["id"], owner_user_id="user-b")["review_status"] == "interrupted"
        assert summary["interrupted"] == 2

    def test_storm_invariant_enqueues_nothing_and_never_calls_ai(self) -> None:
        """HARD INVARIANT: reconcile is a PURE status reconcile -- no enqueue, no AI."""
        repo = InMemoryMatterRepository()
        for _ in range(5):
            _seed_matter(repo, review_status="in_progress", review_started_at="2000-01-01T00:00:00+00:00")

        with (
            patch.object(ingestion_service, "enqueue_on_demand_review") as enqueue,
            patch.object(ingestion_service._INBOUND_REVIEW_POOL, "enqueue") as pool_enqueue,
            patch.object(ingestion_service, "review_nda_with_active_engine") as engine,
        ):
            ingestion_service.reconcile_interrupted_reviews(repository=repo)

        enqueue.assert_not_called()
        pool_enqueue.assert_not_called()
        engine.assert_not_called()

    def test_fail_soft_when_list_matters_raises(self) -> None:
        class _Boom(InMemoryMatterRepository):
            def list_matters(self, owner_user_id: str = "") -> list[dict[str, Any]]:
                raise RuntimeError("disk gone")

        summary = ingestion_service.reconcile_interrupted_reviews(repository=_Boom())
        assert summary == {"scanned": 0, "interrupted": 0, "completed": 0, "errors": 0}


# --------------------------------------------------------------------------- #
# Boot hook fail-safe (server)
# --------------------------------------------------------------------------- #
class TestBootHookFailSafe:
    def test_boot_hook_swallows_errors(self) -> None:
        from nda_automation import server

        with patch.object(
            ingestion_service,
            "reconcile_interrupted_reviews",
            side_effect=RuntimeError("boom"),
        ):
            # Must NOT raise -- the boot hook is wrapped fail-safe.
            server._reconcile_interrupted_reviews()

    def test_boot_hook_invokes_reconcile(self) -> None:
        from nda_automation import server

        with patch.object(
            ingestion_service,
            "reconcile_interrupted_reviews",
            return_value={"scanned": 1, "interrupted": 1, "completed": 0, "errors": 0},
        ) as reconcile:
            server._reconcile_interrupted_reviews()

        reconcile.assert_called_once()


# --------------------------------------------------------------------------- #
# TASK 2 -- pool concurrency bumped to 2
# --------------------------------------------------------------------------- #
class TestPoolConcurrency:
    def test_default_concurrency_is_two(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV, None)
            assert ingestion_service.inbound_review_concurrency() == 2

    def test_env_override_still_honoured(self) -> None:
        import os

        with patch.dict(os.environ, {ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV: "3"}):
            assert ingestion_service.inbound_review_concurrency() == 3
        with patch.dict(os.environ, {ingestion_service.INBOUND_REVIEW_CONCURRENCY_ENV: "0"}):
            assert ingestion_service.inbound_review_concurrency() == 1

    def test_pool_drains_two_jobs_concurrently(self) -> None:
        """Two workers run two jobs in parallel (neither is the head-of-line block)."""
        from nda_automation.ingestion_service import _InboundReviewWorkerPool

        pool = _InboundReviewWorkerPool()
        running = threading.Barrier(2, timeout=5.0)
        completed: list[str] = []
        lock = threading.Lock()

        def _handler(matter_id: str, owner_user_id: str) -> None:
            # If only ONE worker exists, the barrier never reaches 2 and times out.
            running.wait()
            with lock:
                completed.append(matter_id)

        pool.configure(_handler)
        with patch.object(ingestion_service, "inbound_review_concurrency", return_value=2):
            pool.enqueue("m1", "")
            pool.enqueue("m2", "")
            # Both must reach the barrier together; a 1-worker pool would deadlock here.
            deadline = time.monotonic() + 5.0
            while len(completed) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)

        assert sorted(completed) == ["m1", "m2"]


# --------------------------------------------------------------------------- #
# TASK 3 -- per-job wall-clock cap
# --------------------------------------------------------------------------- #
class TestWallClockDeadline:
    def test_deadline_default_is_300(self) -> None:
        import os

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ingestion_service.REVIEW_JOB_DEADLINE_ENV, None)
            assert ingestion_service.review_job_deadline_seconds() == 300.0

    def test_deadline_env_override(self) -> None:
        import os

        with patch.dict(os.environ, {ingestion_service.REVIEW_JOB_DEADLINE_ENV: "12"}):
            assert ingestion_service.review_job_deadline_seconds() == 12.0
        with patch.dict(os.environ, {ingestion_service.REVIEW_JOB_DEADLINE_ENV: "0"}):
            assert ingestion_service.review_job_deadline_seconds() == 0.0

    def test_deadline_fires_and_stamps_interrupted(self) -> None:
        repo = InMemoryMatterRepository()
        matter = _seed_matter(repo, review_status="in_progress")
        release = threading.Event()

        def _slow_engine(text: str, **kwargs: Any) -> dict[str, Any]:
            # Overrun the (tiny) deadline; never persist before the watchdog fires.
            release.wait(timeout=5.0)
            return {"clauses": []}

        import os

        with patch.dict(os.environ, {ingestion_service.REVIEW_JOB_DEADLINE_ENV: "0.2"}):
            ingestion_service._perform_inbound_ai_review_locked(
                matter["id"],
                repository=repo,
                owner_user_id="",
                review_engine_func=_slow_engine,
                is_on_demand=True,
            )

        healed = repo.get_matter(matter["id"])
        assert healed["review_status"] == "interrupted"
        assert "took too long" in healed["review_error"].lower()
        release.set()

    def test_within_deadline_completes_normally(self) -> None:
        repo = InMemoryMatterRepository()
        matter = _seed_matter(repo, review_status="in_progress")

        def _fast_engine(text: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "review_mode": "ai_first",
                "active_review_engine": {"executed_engine": "ai_first"},
                "clauses": [],
            }

        import os

        with patch.dict(os.environ, {ingestion_service.REVIEW_JOB_DEADLINE_ENV: "10"}):
            ingestion_service._perform_inbound_ai_review_locked(
                matter["id"],
                repository=repo,
                owner_user_id="",
                review_engine_func=_fast_engine,
                is_on_demand=True,
            )

        healed = repo.get_matter(matter["id"])
        assert healed["review_status"] == "completed"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
