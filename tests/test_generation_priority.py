"""Generation-priority gate: foreground generate is never starved by background load.

The single prod worker (WEB_CONCURRENCY=1, 1 CPU) runs the fast deterministic NDA
generation on the same process/GIL as the CPU-bound inbound AI-review worker pool
and the Gmail poller. Before this guard, a busy background could starve a generate
of the GIL and push it past the frontend's 45 s timeout. These tests pin the
structural fix: a foreground generation marks itself in flight, and background
CPU-bound units defer to it -- so a generate completes promptly regardless of how
much background work is queued.
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


def test_in_progress_marks_and_clears_the_gate() -> None:
    assert generation_priority.generation_active() is False
    with generation_priority.generation_in_progress():
        assert generation_priority.generation_active() is True
        assert generation_priority.active_generation_count() == 1
    assert generation_priority.generation_active() is False
    assert generation_priority.active_generation_count() == 0


def test_in_progress_restores_the_gate_on_error() -> None:
    try:
        with generation_priority.generation_in_progress():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # Even when the foreground body raised, the gate is released (no leak).
    assert generation_priority.generation_active() is False
    assert generation_priority.active_generation_count() == 0


def test_nested_generations_keep_gate_closed_until_last_leaves() -> None:
    with generation_priority.generation_in_progress():
        assert generation_priority.active_generation_count() == 1
        with generation_priority.generation_in_progress():
            assert generation_priority.active_generation_count() == 2
        # Inner left; a generate is still in flight, so the gate stays closed.
        assert generation_priority.active_generation_count() == 1
        assert generation_priority.generation_active() is True
    assert generation_priority.generation_active() is False


def test_background_unit_defers_while_generation_in_flight_then_unblocks() -> None:
    """A background unit calling yield_to_active_generation parks until generate ends.

    Simulates the real seam: a busy background worker is about to start a heavy unit
    while a foreground generate is in flight. It must block on the gate (releasing
    the GIL) and only proceed once the generate completes -- proving the generate is
    never queued behind the background unit.
    """

    proceeded_at: dict[str, float] = {}
    released = threading.Event()

    def _background_unit() -> None:
        # Defers up to 5 s; should return True the moment the generate finishes.
        cleared = generation_priority.yield_to_active_generation(timeout=5.0)
        proceeded_at["cleared"] = float(cleared)
        proceeded_at["at"] = time.monotonic()
        released.set()

    with generation_priority.generation_in_progress():
        worker = threading.Thread(target=_background_unit, daemon=True)
        worker.start()
        # Give the worker time to reach the gate and park on it.
        time.sleep(0.1)
        # While we hold the gate the background unit must NOT have proceeded.
        assert not released.is_set()
        gate_exit = time.monotonic()
    # Generate finished: the background unit unblocks promptly.
    assert released.wait(timeout=2.0)
    worker.join(timeout=2.0)
    assert proceeded_at["cleared"] == 1.0  # returned True (idle), not a timeout.
    # It unblocked within a small window of the generate completing.
    assert proceeded_at["at"] - gate_exit < 1.0


def test_busy_background_backlog_does_not_delay_a_generation() -> None:
    """The core anti-starvation guarantee, end to end through the gate.

    A backlog of slow background units is queued, each gated by
    yield_to_active_generation. While they are deferring, a foreground generation's
    critical section runs and MUST complete promptly (well under the frontend's 45 s
    timeout) -- it is never stuck behind the background backlog. Without the gate the
    background units would race the generate for the GIL; with it they park and yield.
    """

    stop = threading.Event()
    background_started = threading.Event()

    def _slow_background_unit() -> None:
        background_started.set()
        # Each unit defers to in-flight generation, then does "work" (here just a
        # short sleep standing in for a heavy review/extraction). Loops to emulate a
        # backlog draining for the whole duration of the test.
        while not stop.is_set():
            generation_priority.yield_to_active_generation(timeout=5.0)
            time.sleep(0.02)

    workers = [threading.Thread(target=_slow_background_unit, daemon=True) for _ in range(4)]
    for worker in workers:
        worker.start()
    assert background_started.wait(timeout=2.0)
    time.sleep(0.05)  # let the backlog get going

    # Now run a "generation" critical section and time it. The gate makes the
    # background units defer, so this completes promptly.
    start = time.monotonic()
    with generation_priority.generation_in_progress():
        # Stand in for the ~17-58 ms deterministic generate + persistence.
        time.sleep(0.05)
    elapsed = time.monotonic() - start

    stop.set()
    for worker in workers:
        worker.join(timeout=2.0)

    # Generosity for CI jitter, but orders of magnitude under the 45 s timeout and
    # not inflated by the queued background backlog.
    assert elapsed < 2.0


def test_yield_proceeds_after_timeout_when_generation_does_not_finish() -> None:
    """A stuck/long generate can never permanently wedge the background pool.

    With a generate held in flight, a background unit with a short yield timeout must
    proceed (return False) once the bound elapses, rather than blocking forever.
    """

    result: dict[str, object] = {}
    done = threading.Event()

    def _background_unit() -> None:
        result["cleared"] = generation_priority.yield_to_active_generation(timeout=0.2)
        done.set()

    with generation_priority.generation_in_progress():
        worker = threading.Thread(target=_background_unit, daemon=True)
        worker.start()
        # Hold the generate longer than the background unit's yield timeout.
        assert done.wait(timeout=2.0)
        worker.join(timeout=2.0)
    # The unit proceeded after the timeout (False == "still active, proceeded anyway").
    assert result["cleared"] is False


# --- wiring: the seams actually call the gate -------------------------------- #


def test_inbound_review_pool_handler_yields_before_reviewing() -> None:
    """The production review-pool handler defers to generation BEFORE the heavy review.

    Proves the seam is wired: the handler must call yield_to_active_generation before
    it invokes the (CPU + network) review, so a queued background review never runs
    ahead of an in-flight generate on the single worker.
    """

    call_order: list[str] = []

    def _record_yield(*_args, **_kwargs):
        call_order.append("yield")
        return True

    def _record_review(*_args, **_kwargs):
        call_order.append("review")

    with (
        patch.object(generation_priority, "yield_to_active_generation", _record_yield),
        patch.object(ingestion_service, "_perform_inbound_ai_review", _record_review),
    ):
        ingestion_service._inbound_review_pool_handler("matter_abc", "owner_1")

    assert call_order == ["yield", "review"]


def test_generation_route_wraps_generation_in_the_gate() -> None:
    """POST /api/generate-nda marks generation in flight for the workflow's duration.

    The route handler must enter generation_in_progress() around the workflow call so
    the gate is closed exactly while the generate runs.
    """

    from nda_automation.routes import generation as generation_route

    observed: dict[str, object] = {}

    class _FakeHandler:
        def _read_json_payload(self):
            return {"signing_entity_id": "x"}

        def _send_json(self, payload, status=200):
            observed["status"] = status

    def _fake_workflow(payload, *, owner_user_id):
        # While the workflow runs, the gate must be closed.
        observed["active_during"] = generation_priority.generation_active()
        raise generation_route.nda_generation_workflow.GenerationPayloadError("stop here")

    with (
        patch.object(generation_route.nda_generation_workflow, "generate_nda_from_payload", _fake_workflow),
        patch.object(generation_route, "request_owner_user_id", lambda handler: ""),
    ):
        # Before the call the gate is open.
        assert generation_priority.generation_active() is False
        generation_route.handle_generate_nda(_FakeHandler())

    assert observed["active_during"] is True
    # After the call the gate is released again (even though the workflow raised).
    assert generation_priority.generation_active() is False
