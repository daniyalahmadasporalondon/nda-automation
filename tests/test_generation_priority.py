"""Generation-priority gate: foreground generate is never starved by background load.

The single prod worker (WEB_CONCURRENCY=1, 1 CPU) runs the fast deterministic NDA
generation on the same process/GIL as the CPU-bound inbound AI-review worker pool
and the Gmail poller. Before this guard, a busy background could starve a generate
of the GIL and push it past the frontend's 45 s timeout.

The shared contract these tests pin (other builders import it):

* ``generation_in_progress() -> bool`` -- True while any generate is in flight.
* ``generation_in_progress_guard()`` -- context manager that marks a generate in
  flight for its duration (re-entrant counter, always clears in finally).
* ``should_defer_background_ai() -> bool`` -- True when background AI review must
  NOT start now (currently: while a generate is in flight). Fail-open.

The legacy ``yield_to_active_generation`` soft-yield is retained for the Gmail
poller and still covered below.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

from nda_automation import generation_priority, ingestion_service


def test_yield_returns_immediately_when_no_generation_active() -> None:
    # With nothing in flight the background path is never delayed.
    start = time.monotonic()
    assert generation_priority.yield_to_active_generation(timeout=5.0) is True
    assert time.monotonic() - start < 0.2
    assert generation_priority.generation_active() is False
    assert generation_priority.active_generation_count() == 0


def test_generation_in_progress_is_a_bool_predicate() -> None:
    # The spec'd accessor is a plain bool function, not a context manager.
    assert generation_priority.generation_in_progress() is False
    with generation_priority.generation_in_progress_guard():
        assert generation_priority.generation_in_progress() is True
    assert generation_priority.generation_in_progress() is False


def test_guard_marks_and_clears_the_gate() -> None:
    assert generation_priority.generation_active() is False
    with generation_priority.generation_in_progress_guard():
        assert generation_priority.generation_active() is True
        assert generation_priority.active_generation_count() == 1
    assert generation_priority.generation_active() is False
    assert generation_priority.active_generation_count() == 0


def test_guard_restores_the_gate_on_error() -> None:
    try:
        with generation_priority.generation_in_progress_guard():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # Even when the foreground body raised, the gate is released (no leak).
    assert generation_priority.generation_active() is False
    assert generation_priority.active_generation_count() == 0


def test_nested_generations_keep_gate_closed_until_last_leaves() -> None:
    with generation_priority.generation_in_progress_guard():
        assert generation_priority.active_generation_count() == 1
        with generation_priority.generation_in_progress_guard():
            assert generation_priority.active_generation_count() == 2
        # Inner left; a generate is still in flight, so the gate stays closed.
        assert generation_priority.active_generation_count() == 1
        assert generation_priority.generation_active() is True
    assert generation_priority.generation_active() is False


def test_concurrent_guards_are_thread_safe() -> None:
    """N concurrent generates keep the gate closed until the LAST one leaves."""
    enter = threading.Barrier(4 + 1)
    release = threading.Event()

    def _hold() -> None:
        with generation_priority.generation_in_progress_guard():
            enter.wait(timeout=2.0)
            release.wait(timeout=2.0)

    workers = [threading.Thread(target=_hold, daemon=True) for _ in range(4)]
    for w in workers:
        w.start()
    enter.wait(timeout=2.0)  # all 4 are inside their guard now
    assert generation_priority.active_generation_count() == 4
    assert generation_priority.generation_in_progress() is True
    release.set()
    for w in workers:
        w.join(timeout=2.0)
    assert generation_priority.active_generation_count() == 0
    assert generation_priority.generation_in_progress() is False


# --- should_defer_background_ai: the predicate background units check ---------- #


def test_should_defer_is_false_when_idle() -> None:
    assert generation_priority.generation_in_progress() is False
    assert generation_priority.should_defer_background_ai() is False


def test_should_defer_is_true_while_generation_in_flight_then_false_after() -> None:
    # (a) background review defers when a generation is in progress, resumes after.
    with generation_priority.generation_in_progress_guard():
        assert generation_priority.should_defer_background_ai() is True
    assert generation_priority.should_defer_background_ai() is False


def test_should_defer_is_fail_open_on_error() -> None:
    # A bug in the predicate must never block background work -> defaults to False.
    with patch.object(
        generation_priority, "generation_in_progress", side_effect=RuntimeError("boom")
    ):
        assert generation_priority.should_defer_background_ai() is False


# --- legacy soft-yield (Gmail poller path) ----------------------------------- #


def test_background_unit_defers_while_generation_in_flight_then_unblocks() -> None:
    """A background unit calling yield_to_active_generation parks until generate ends."""

    proceeded_at: dict[str, float] = {}
    released = threading.Event()

    def _background_unit() -> None:
        cleared = generation_priority.yield_to_active_generation(timeout=5.0)
        proceeded_at["cleared"] = float(cleared)
        proceeded_at["at"] = time.monotonic()
        released.set()

    with generation_priority.generation_in_progress_guard():
        worker = threading.Thread(target=_background_unit, daemon=True)
        worker.start()
        time.sleep(0.1)
        assert not released.is_set()
        gate_exit = time.monotonic()
    assert released.wait(timeout=2.0)
    worker.join(timeout=2.0)
    assert proceeded_at["cleared"] == 1.0  # returned True (idle), not a timeout.
    assert proceeded_at["at"] - gate_exit < 1.0


def test_yield_proceeds_after_timeout_when_generation_does_not_finish() -> None:
    """A stuck/long generate can never permanently wedge the legacy yield path."""

    result: dict[str, object] = {}
    done = threading.Event()

    def _background_unit() -> None:
        result["cleared"] = generation_priority.yield_to_active_generation(timeout=0.2)
        done.set()

    with generation_priority.generation_in_progress_guard():
        worker = threading.Thread(target=_background_unit, daemon=True)
        worker.start()
        assert done.wait(timeout=2.0)
        worker.join(timeout=2.0)
    assert result["cleared"] is False


# --- wiring: the review-pool handler refuses to start under generation -------- #


def test_inbound_review_pool_handler_defers_and_requeues_under_generation() -> None:
    """The review-pool handler REFUSES to start the review while a generate runs.

    Right-of-way contract: with a generation in flight the handler must NOT call
    the heavy review, and must re-queue the matter (never drop it) so it retries
    once the generate clears.
    """

    reviewed: list[str] = []
    requeued: list[tuple[str, str]] = []

    def _record_review(*_args, **_kwargs):
        reviewed.append("review")

    def _record_requeue(matter_id, owner_user_id, delay):  # noqa: ARG001
        requeued.append((matter_id, owner_user_id))

    with (
        patch.object(ingestion_service, "_perform_inbound_ai_review", _record_review),
        patch.object(
            ingestion_service._INBOUND_REVIEW_POOL,
            "requeue_after_backoff",
            _record_requeue,
        ),
        generation_priority.generation_in_progress_guard(),
    ):
        ingestion_service._inbound_review_pool_handler("matter_abc", "owner_1")

    assert reviewed == []  # heavy review refused to start.
    assert requeued == [("matter_abc", "owner_1")]  # matter re-queued, not lost.


def test_inbound_review_pool_handler_reviews_when_idle() -> None:
    """With no generation in flight the handler runs the review immediately."""

    reviewed: list[str] = []

    with patch.object(
        ingestion_service, "_perform_inbound_ai_review", lambda *a, **k: reviewed.append("review")
    ):
        ingestion_service._inbound_review_pool_handler("matter_abc", "owner_1")

    assert reviewed == ["review"]
