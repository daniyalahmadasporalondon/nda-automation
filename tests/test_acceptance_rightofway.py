"""INDEPENDENT adversarial acceptance gate for the "Generate right-of-way +
kill-switch + loop-closer" hardening (branch ``fix/generate-right-of-way``).

These tests are written against the *target* behaviour the builder must satisfy,
NOT the code as it stands on the base SHA. On the base they are expected to
FAIL/ERROR -- the integrator runs them against the MERGED branch as the gate.

Where a target symbol/endpoint does not exist yet, the test imports it lazily and
calls ``pytest.fail(...)`` with an explicit message so it fails *clearly* (a
graded gate result) rather than erroring on collection/import.

The four target behaviours under test (one section each):

1. RIGHT OF WAY  -- while a foreground generation is in progress, background AI
   review work must NOT start: it defers/requeues, LOSES NO item, and resumes
   after the guard exits. Boundary: an item enqueued *during* a generation is
   processed only after the guard exits.

2. KILL SWITCH AT DRAIN TIME -- with NDA_INBOUND_AI_REVIEW_ENABLED=false, the
   inbound review worker handler must early-return WITHOUT running an AI review,
   even for items ALREADY in the queue (flag flipped after enqueue, checked at
   DRAIN, not only at enqueue).

3. LOOP-CLOSER -- when the persist step (update_matter_review) returns
   None/falsy, it must be COUNTED as a failure (poison-pill counter increments),
   and after the failure cap (3) the matter must NOT be re-enqueued by the
   recovery sweep.

4. FAIL-OPEN -- should_defer_background_ai() and the guards must fail-open: a
   bug/exception in the priority check must NOT permanently block review work.
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nda_automation import generation_priority, ingestion_service, matter_store
from nda_automation.matter_repository import DiskMatterRepository, InMemoryMatterRepository


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ai_first_review_result() -> dict[str, Any]:
    """A review-result dict marked as the ai_first engine (idempotency marker)."""
    return {
        "review_mode": "ai_first",
        "active_review_engine": {"executed_engine": "ai_first"},
        "clauses": [],
        "requirements_passed": 1,
        "requirements_needs_review": 0,
        "requirements_failed": 0,
    }


def _stub_ai_engine(calls: list[str] | None = None):
    """A review-engine stub that records each call and returns an ai_first result."""
    recorded = calls if calls is not None else []

    def _engine(text, *, paragraphs=None, **_kwargs):
        recorded.append(text)
        return _ai_first_review_result()

    _engine.calls = recorded  # type: ignore[attr-defined]
    return _engine


def _seed_inbound_matter(
    repository: InMemoryMatterRepository,
    *,
    owner_user_id: str = "owner-1",
    extracted_text: str = "This Mutual NDA is governed by the laws of England and Wales.",
    source_type: str = "gmail_inbound",
) -> dict[str, Any]:
    """Create a deterministic-first-pass inbound matter directly in the repo.

    Mirrors the post-import state: executed_engine == "deterministic" (NOT yet
    AI-reviewed) so the scheduler/sweep/worker all consider it eligible.
    """
    return repository.create_matter(
        source_filename="inbound.docx",
        document_bytes=b"PK-stub-bytes",
        extracted_text=extracted_text,
        review_result={
            "review_mode": "deterministic",
            "active_review_engine": {"executed_engine": "deterministic"},
            "clauses": [],
        },
        triage={},
        source_type=source_type,
        owner_user_id=owner_user_id,
    )


def _reset_generation_gate() -> None:
    """Force the process-wide generation gate back to idle (test isolation).

    A previous test that left a generation 'in progress' (or a partially-built
    guard) must never bleed into the next test. We drain the counter and set the
    idle event directly so each test starts from a known-idle state.
    """
    try:  # best-effort: internals may be refactored by the builder.
        with generation_priority._LOCK:  # type: ignore[attr-defined]
            generation_priority._active_count = 0  # type: ignore[attr-defined]
            generation_priority._idle_event.set()  # type: ignore[attr-defined]
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    """Per-test isolation: idle generation gate."""
    _reset_generation_gate()
    yield
    _reset_generation_gate()


def _resolve_should_defer():
    """Return generation_priority.should_defer_background_ai or fail clearly.

    Target symbol introduced by the right-of-way build. Imported lazily so the
    suite *collects* on the base SHA and produces a graded failure instead of an
    import error.
    """
    fn = getattr(generation_priority, "should_defer_background_ai", None)
    if not callable(fn):
        pytest.fail(
            "TARGET NOT IMPLEMENTED: generation_priority.should_defer_background_ai() "
            "is missing. The right-of-way build must add a fail-open predicate the "
            "inbound review worker consults at DRAIN time to defer/requeue while a "
            "foreground generation is in flight."
        )
    return fn


def _resolve_in_progress_guard():
    """Return the generation guard context manager the spec names.

    Spec names ``generation_priority.generation_in_progress_guard()``. Accept the
    pre-existing ``generation_in_progress`` as an alias so a builder who merely
    renamed/aliased still passes; fail clearly only if NEITHER exists.
    """
    for name in ("generation_in_progress_guard", "generation_in_progress"):
        guard = getattr(generation_priority, name, None)
        if callable(guard):
            return guard
    pytest.fail(
        "TARGET NOT IMPLEMENTED: neither generation_priority.generation_in_progress_guard() "
        "nor generation_in_progress() exists -- the foreground generate has no way to mark "
        "right-of-way."
    )


# =========================================================================== #
# 1. RIGHT OF WAY
# =========================================================================== #
class TestRightOfWay:
    def test_should_defer_is_true_inside_the_guard_and_false_outside(self):
        """should_defer_background_ai() tracks the guard exactly at the boundary."""
        should_defer = _resolve_should_defer()
        guard = _resolve_in_progress_guard()

        assert should_defer() is False, "must NOT defer when no generation is in flight"
        with guard():
            assert should_defer() is True, "must defer WHILE a generation is in flight"
        assert should_defer() is False, "must resume (stop deferring) after the guard exits"

    def test_item_enqueued_during_generation_is_processed_only_after_guard_exits(self):
        """The core boundary: an item enqueued DURING a generation is held, loses
        nothing, and is reviewed only AFTER the generate completes.

        We drive the worker handler directly (not the live daemon pool) so the
        test is deterministic. The handler MUST consult right-of-way at drain
        time: while the guard is open it must NOT run the AI engine; once the
        guard closes the SAME item must be reviewed (no loss).
        """
        should_defer = _resolve_should_defer()
        guard = _resolve_in_progress_guard()

        repository = InMemoryMatterRepository()
        matter = _seed_inbound_matter(repository)
        matter_id = matter["id"]
        engine = _stub_ai_engine()

        review_started = threading.Event()
        review_finished = threading.Event()

        def _run_review():
            # The target behaviour: the worker defers (busy-waits/parks) while a
            # generation is in flight, then performs the review once clear. We
            # emulate the worker's drain loop using the public defer predicate so
            # this test does not couple to a private requeue mechanism.
            while should_defer():
                time.sleep(0.005)
            review_started.set()
            ingestion_service._perform_inbound_ai_review(
                matter_id,
                repository=repository,
                owner_user_id="owner-1",
                review_engine_func=engine,
                use_semaphore=False,
            )
            review_finished.set()

        with guard():
            worker = threading.Thread(target=_run_review, daemon=True)
            worker.start()
            # While the guard is open the review must NOT have started, and NO AI
            # call may have fired -- right of way belongs to the (absent) generate.
            time.sleep(0.1)
            assert not review_started.is_set(), "review started while a generation held right-of-way"
            assert engine.calls == [], "AI engine ran while a generation was in flight"  # type: ignore[attr-defined]
            # The matter is still un-AI-reviewed: nothing was lost, nothing ran.
            still = repository.get_matter(matter_id, owner_user_id="owner-1")
            assert still["review_result"]["active_review_engine"]["executed_engine"] == "deterministic"

        # Guard exited -> the deferred item must now be processed (NOT dropped).
        assert review_finished.wait(timeout=5.0), "deferred review never resumed after the guard exited"
        worker.join(timeout=5.0)
        assert engine.calls, "the deferred item was LOST -- AI review never ran after the guard exited"  # type: ignore[attr-defined]
        reviewed = repository.get_matter(matter_id, owner_user_id="owner-1")
        assert reviewed["review_result"]["active_review_engine"]["executed_engine"] == "ai_first"

    def test_concurrent_generations_keep_right_of_way_until_the_last_one_exits(self):
        """Right-of-way is a COUNTER, not a boolean: two overlapping generations
        keep deferral on until BOTH exit (a boolean would clear on the first)."""
        should_defer = _resolve_should_defer()
        guard = _resolve_in_progress_guard()

        with guard():
            assert should_defer() is True
            with guard():
                assert should_defer() is True
            # Inner generate left, outer still in flight -> still defer.
            assert should_defer() is True, (
                "right-of-way cleared after only ONE of two concurrent generations exited "
                "(boolean instead of counter)"
            )
        assert should_defer() is False


# =========================================================================== #
# 2. NO KILL-SWITCH AT DRAIN: an on-demand review always runs (it is the only
#    enqueuer; there is no inbound auto-review path or kill-switch to skip it).
# =========================================================================== #
class TestReviewRunsAtDrain:
    def test_review_runs_at_drain_with_no_kill_switch(self):
        """The drain path runs the AI review unconditionally -- there is no longer a
        kill-switch that could skip an already-queued (on-demand) review."""
        repository = InMemoryMatterRepository()
        matter = _seed_inbound_matter(repository)
        engine = _stub_ai_engine()
        _reset_generation_gate()  # no generation in flight to mask the run

        ingestion_service._perform_inbound_ai_review(
            matter["id"],
            repository=repository,
            owner_user_id="owner-1",
            review_engine_func=engine,
            use_semaphore=False,
        )
        assert engine.calls, "the drain path must run the AI review (no kill-switch)"  # type: ignore[attr-defined]


# =========================================================================== #
# 3. LOOP-CLOSER (None-return persist counts as a failure; cap stops the sweep)
# =========================================================================== #
class _NonePersistRepository(InMemoryMatterRepository):
    """An in-memory repo whose review persist ALWAYS returns None (falsy).

    Emulates the persist step silently failing (the matter vanished, an owner
    mismatch, a concurrent prune) -- the exact path the loop-closer must treat as
    a failure. update_matter_fields still works so the poison-pill counter can be
    recorded and read back. The async worker persists via the guarded
    refresh_matter_review, so that is the method stubbed null here (the unguarded
    update_matter_review is stubbed too for completeness).
    """

    def __init__(self) -> None:
        super().__init__()
        self.review_calls = 0

    def refresh_matter_review(self, matter_id, review_result, triage, *, expected_updated_at="", owner_user_id=""):
        self.review_calls += 1
        return None

    def update_matter_review(self, matter_id, review_result, triage, owner_user_id=""):
        self.review_calls += 1
        return None


class TestLoopCloser:
    def test_none_persist_increments_the_failure_counter(self):
        """A single None-return from update_matter_review must bump
        inbound_review_failures by one (today it silently `return`s and never
        records a failure)."""
        repository = _NonePersistRepository()
        matter = _seed_inbound_matter(repository)
        matter_id = matter["id"]
        engine = _stub_ai_engine()

        before = ingestion_service._matter_review_failure_count(
            repository.get_matter(matter_id, owner_user_id="owner-1")
        )
        assert before == 0

        ingestion_service._perform_inbound_ai_review(
            matter_id,
            repository=repository,
            owner_user_id="owner-1",
            review_engine_func=engine,
            use_semaphore=False,
        )

        after = ingestion_service._matter_review_failure_count(
            repository.get_matter(matter_id, owner_user_id="owner-1")
        )
        assert after == before + 1, (
            "LOOP-CLOSER HOLE: a None/falsy return from update_matter_review was NOT counted "
            "as a failure. The persist-None path must increment the poison-pill counter so a "
            "permanently-unpersistable matter eventually stops being re-enqueued."
        )

    def test_none_persist_releases_the_concurrency_slot(self):
        """The None-return path must also RELEASE the concurrency bound (semaphore
        slot) -- a persist-None that leaked the slot would starve the pool after a
        few poison pills. We exercise the semaphore-gated path repeatedly and
        assert the bound is never exhausted (each call must acquire AND release)."""
        repository = _NonePersistRepository()
        matter = _seed_inbound_matter(repository)
        matter_id = matter["id"]
        engine = _stub_ai_engine()

        # The default concurrency bound is 1; if a slot leaked on the None path,
        # the SECOND semaphore-gated call would block forever. Run several with a
        # watchdog so a leak surfaces as a timeout, not a hang.
        done = threading.Event()

        def _hammer():
            for _ in range(4):
                ingestion_service._perform_inbound_ai_review(
                    matter_id,
                    repository=repository,
                    owner_user_id="owner-1",
                    review_engine_func=engine,
                    use_semaphore=True,  # take the semaphore-gated path explicitly.
                )
            done.set()

        threading.Thread(target=_hammer, daemon=True).start()
        assert done.wait(timeout=10.0), (
            "LOOP-CLOSER HOLE: a None-return persist leaked the concurrency slot -- the "
            "semaphore-gated review path deadlocked after repeated poison pills."
        )


# =========================================================================== #
# 4. FAIL-OPEN
# =========================================================================== #
class TestFailOpen:
    def test_should_defer_fails_open_when_the_priority_check_raises(self, monkeypatch):
        """A bug/exception inside the priority check must NOT permanently block
        review work: should_defer_background_ai() must swallow and return False
        (proceed) rather than propagate or wedge."""
        should_defer = _resolve_should_defer()

        # Force the underlying liveness read to blow up. We patch the most likely
        # internal the predicate consults; if the builder named it differently the
        # predicate must STILL not raise (that is the whole point of fail-open).
        def _boom(*_a, **_k):
            raise RuntimeError("injected priority-check bug")

        monkeypatch.setattr(generation_priority, "generation_active", _boom, raising=False)
        monkeypatch.setattr(generation_priority, "active_generation_count", _boom, raising=False)

        try:
            result = should_defer()
        except Exception as exc:  # pragma: no cover - explicit failure message
            pytest.fail(
                "FAIL-OPEN HOLE: should_defer_background_ai() propagated an exception "
                f"({exc!r}) from a buggy priority check. It must swallow and return False."
            )
        assert result is False, (
            "FAIL-OPEN HOLE: with the priority check raising, should_defer_background_ai() "
            "must return False (proceed), never True (which would permanently block reviews)."
        )

    def test_review_still_runs_when_priority_check_is_broken(self, monkeypatch):
        """End-to-end fail-open: even if the right-of-way machinery is broken, an
        inbound review must still execute (deferral failing-open == proceed)."""
        # Break the gate internals so any guard read raises.
        def _boom(*_a, **_k):
            raise RuntimeError("injected gate bug")

        monkeypatch.setattr(generation_priority, "generation_active", _boom, raising=False)
        monkeypatch.setattr(generation_priority, "active_generation_count", _boom, raising=False)
        if hasattr(generation_priority, "should_defer_background_ai"):
            # Leave should_defer itself in place so its OWN fail-open is exercised;
            # we only broke the internals it reads.
            pass

        repository = InMemoryMatterRepository()
        matter = _seed_inbound_matter(repository)
        engine = _stub_ai_engine()

        ingestion_service._perform_inbound_ai_review(
            matter["id"],
            repository=repository,
            owner_user_id="owner-1",
            review_engine_func=engine,
            use_semaphore=False,
        )
        assert engine.calls, (
            "FAIL-OPEN HOLE: a broken priority check permanently blocked the inbound review "
            "(the review never ran). Deferral must fail-open to PROCEED."
        )

    def test_yield_to_active_generation_fails_open(self):
        """Pre-existing fail-open invariant the build must not regress: a broken
        yield path returns True (clear) rather than raising/blocking."""
        # The contract: any unexpected error returns True (proceed). We can at least
        # assert the no-generation fast-path is immediate and truthy.
        _reset_generation_gate()
        assert generation_priority.yield_to_active_generation(timeout=0.01) is True


# =========================================================================== #
# 5. PERSIST-POINT CONTENTION (the d054bfb slow-Generate root cause)
# =========================================================================== #
def _resolve_yield_store():
    """Return generation_priority.yield_store_to_generation or fail clearly.

    Target symbol introduced by fix/protect-generate-persist: the persist-point
    yield a background review writer calls right before its store WRITE so a
    concurrent foreground generate's save acquires the single global store lock
    first. Imported lazily so the suite collects on the base SHA with a graded
    failure instead of an import error.
    """
    fn = getattr(generation_priority, "yield_store_to_generation", None)
    if not callable(fn):
        pytest.fail(
            "TARGET NOT IMPLEMENTED: generation_priority.yield_store_to_generation() "
            "is missing. The drain-time gate only defers review START; a review that "
            "was already mid-flight when a Generate began still grabs the store lock at "
            "PERSIST time and starves the generate's save. The fix must add a bounded, "
            "fail-open persist-point yield the review worker consults before its write."
        )
    return fn


class TestPersistPointContention:
    """The d054bfb defect: a Generate's 'matter persisted' phase took 147s because
    its several store writes contended on the single global lock with a concurrent
    verifier storm. The drain-time right-of-way gate does NOT cover a review that
    already STARTED before the Generate -- that review finishes its slow, lock-free
    assessor+verifier work and then beats the generate to the lock at PERSIST time.
    These tests pin the persist-point yield that closes the gap.
    """

    def test_yield_store_parks_while_generation_in_flight_and_clears_after(self):
        """yield_store_to_generation() returns at once when idle, parks while a
        generation holds right-of-way, and proceeds (fail-open) after it exits."""
        yield_store = _resolve_yield_store()
        guard = _resolve_in_progress_guard()

        _reset_generation_gate()
        # Idle: the write may proceed immediately.
        assert yield_store(timeout=0.01) is True

        with guard():
            # In flight: a short-timeout yield must NOT report clear -- it parked the
            # whole (short) window and a generation was still in flight throughout.
            t0 = time.monotonic()
            cleared = yield_store(timeout=0.05)
            assert (time.monotonic() - t0) >= 0.04, "yield returned without parking"
            assert cleared is False, "yield reported clear while a generation held right-of-way"
        # Guard exited: clear again.
        assert yield_store(timeout=0.01) is True

    def test_review_persist_is_held_until_the_generate_releases(self):
        """The inbound review's PERSIST must stand back while a generation is in
        flight: the AI work may finish, but the store write does not land until the
        generate's guard exits. Nothing is lost -- the write lands right after."""
        _resolve_yield_store()
        guard = _resolve_in_progress_guard()

        repository = InMemoryMatterRepository()
        matter = _seed_inbound_matter(repository)
        matter_id = matter["id"]
        engine = _stub_ai_engine()

        persisted = threading.Event()

        def _run_review():
            ingestion_service._perform_inbound_ai_review(
                matter_id,
                repository=repository,
                owner_user_id="owner-1",
                review_engine_func=engine,
                use_semaphore=False,
            )
            persisted.set()

        with guard():
            worker = threading.Thread(target=_run_review, daemon=True)
            worker.start()
            # Give the worker time to run the (stub) AI work and REACH the persist
            # yield. The AI call fires (it started before/independent of the gate),
            # but the store write must be parked behind the generate.
            time.sleep(0.2)
            assert engine.calls, "stub engine never ran"  # type: ignore[attr-defined]
            assert not persisted.is_set(), (
                "PERSIST-POINT HOLE: the review's store write completed WHILE a "
                "generation held right-of-way -- the persist did not yield the lock "
                "window to the foreground generate."
            )
            still = repository.get_matter(matter_id, owner_user_id="owner-1")
            assert still["review_result"]["active_review_engine"]["executed_engine"] == "deterministic"

        # Guard exited -> the held write must now land (no loss).
        assert persisted.wait(timeout=5.0), "the parked review persist never completed after the guard exited"
        worker.join(timeout=5.0)
        reviewed = repository.get_matter(matter_id, owner_user_id="owner-1")
        assert reviewed["review_result"]["active_review_engine"]["executed_engine"] == "ai_first", (
            "the review result was LOST -- the parked persist never wrote after the guard exited"
        )

    def test_lower_entry_defers_and_requeues_when_generation_active(self):
        """The defer check runs DEEPER than the pool handler: inside the concurrency
        bound, just before the heavy AI work. A review that passed the front gate
        but is about to start while a generate raced in must defer + requeue (via the
        injected on_defer) and NOT run the engine -- the matter is never dropped."""
        _resolve_yield_store()
        guard = _resolve_in_progress_guard()

        repository = InMemoryMatterRepository()
        matter = _seed_inbound_matter(repository)
        matter_id = matter["id"]
        engine = _stub_ai_engine()
        requeued: list[str] = []

        with guard():
            ingestion_service._perform_inbound_ai_review(
                matter_id,
                repository=repository,
                owner_user_id="owner-1",
                review_engine_func=engine,
                use_semaphore=False,
                on_defer=lambda: requeued.append(matter_id),
            )

        assert engine.calls == [], (  # type: ignore[attr-defined]
            "DEEP-DEFER HOLE: the review's AI engine ran inside the bound while a "
            "generation held right-of-way -- the lower entry did not defer."
        )
        assert requeued == [matter_id], "the deferred item was not re-queued (it would be lost)"
        still = repository.get_matter(matter_id, owner_user_id="owner-1")
        assert still["review_result"]["active_review_engine"]["executed_engine"] == "deterministic"

    def test_lower_entry_without_requeue_falls_through(self):
        """A caller WITHOUT a requeue (tests / explicit runner) must NOT be blocked
        by the deep defer -- it falls through and runs (the persist-point yield still
        protects generation's save). Otherwise such a review would be silently lost."""
        _resolve_yield_store()
        guard = _resolve_in_progress_guard()

        repository = InMemoryMatterRepository()
        matter = _seed_inbound_matter(repository)
        matter_id = matter["id"]
        engine = _stub_ai_engine()

        done = threading.Event()

        def _run():
            ingestion_service._perform_inbound_ai_review(
                matter_id,
                repository=repository,
                owner_user_id="owner-1",
                review_engine_func=engine,
                use_semaphore=False,
                on_defer=None,  # no requeue -> must fall through, not drop.
            )
            done.set()

        with guard():
            threading.Thread(target=_run, daemon=True).start()
            # The persist-point yield will park the WRITE, but the engine runs.
            time.sleep(0.2)
            assert engine.calls, (  # type: ignore[attr-defined]
                "a review with no requeue was dropped by the deep defer instead of falling through"
            )
        assert done.wait(timeout=5.0)
        reviewed = repository.get_matter(matter_id, owner_user_id="owner-1")
        assert reviewed["review_result"]["active_review_engine"]["executed_engine"] == "ai_first"

    def test_raise_if_generation_active_and_defer_payload(self):
        """The exception-driven twin: raise_if_generation_active() raises
        ForegroundGenerationDeferred while a generate is in flight (carrying the
        defer payload) and is a no-op otherwise; fail-open on a broken check."""
        guard = _resolve_in_progress_guard()
        exc_cls = getattr(generation_priority, "ForegroundGenerationDeferred", None)
        raise_if = getattr(generation_priority, "raise_if_generation_active", None)
        payload_fn = getattr(generation_priority, "generation_defer_payload", None)
        if not (exc_cls and callable(raise_if) and callable(payload_fn)):
            pytest.fail(
                "TARGET NOT IMPLEMENTED: generation_priority must expose "
                "ForegroundGenerationDeferred + raise_if_generation_active() + "
                "generation_defer_payload()."
            )

        _reset_generation_gate()
        # Idle: no-op, and the payload reports not-deferred-ish (active 0).
        raise_if()  # must not raise
        assert payload_fn()["active_generations"] == 0

        with guard():
            with pytest.raises(exc_cls) as caught:
                raise_if()
            assert caught.value.payload["deferred"] is True
            assert caught.value.payload["active_generations"] >= 1

    def test_generate_save_stays_fast_under_a_verifier_storm(self):
        """End-to-end, REAL disk store: a verifier-storm of review writers is active
        and persisting on the single global _locked_store, a Generate's matter-save
        (create_matter) runs concurrently, and it completes FAST (well under a
        second) -- not blocked for multi-second behind the review writes.

        Without the persist-point yield the review writers race the generate for the
        non-fair lock and the save can stall; with it they stand back while the
        generate's guard is in flight, so create_matter wins the lock immediately.
        """
        _resolve_yield_store()
        guard = _resolve_in_progress_guard()

        with tempfile.TemporaryDirectory() as data_dir:
            root = Path(data_dir)
            with (
                patch.object(matter_store, "DATA_DIR", root),
                patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
                patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
            ):
                repository = DiskMatterRepository()

                # Seed several deterministic-first inbound matters to review.
                review_targets = [
                    _seed_inbound_matter(
                        repository,
                        owner_user_id="owner-storm",
                        extracted_text=f"Inbound NDA number {n} governed by England and Wales.",
                    )["id"]
                    for n in range(4)
                ]

                # A review engine whose 'AI work' is a short, lock-free latency --
                # standing in for the real slow assessor+verifier. Each writer then
                # hits the real disk _locked_store at its persist.
                def _slow_engine(text, *, paragraphs=None, **_kwargs):
                    time.sleep(0.05)
                    return _ai_first_review_result()

                stop = threading.Event()

                def _storm(matter_id):
                    # Hammer the persist path repeatedly to keep lock pressure high
                    # for the whole window the generate is trying to save.
                    while not stop.is_set():
                        ingestion_service._perform_inbound_ai_review(
                            matter_id,
                            repository=repository,
                            owner_user_id="owner-storm",
                            review_engine_func=_slow_engine,
                            use_semaphore=False,
                        )

                _reset_generation_gate()
                storm_threads = [
                    threading.Thread(target=_storm, args=(mid,), daemon=True)
                    for mid in review_targets
                ]
                for thread in storm_threads:
                    thread.start()
                try:
                    # Let the storm get into its persist/lock churn first.
                    time.sleep(0.1)

                    # Now a foreground Generate's matter-save, under the right-of-way
                    # guard exactly as the real route wraps it. Time JUST the save.
                    with guard():
                        start = time.monotonic()
                        created = repository.create_matter(
                            source_filename="NDA - Counterparty.docx",
                            document_bytes=b"PK-generated-bytes",
                            extracted_text="Generated mutual NDA governed by England and Wales.",
                            review_result={
                                "review_mode": "deterministic",
                                "active_review_engine": {"executed_engine": "deterministic"},
                                "clauses": [],
                            },
                            triage={},
                            source_type="generated",
                            board_column="generated",
                            owner_user_id="owner-generate",
                        )
                        elapsed = time.monotonic() - start
                finally:
                    stop.set()
                    for thread in storm_threads:
                        thread.join(timeout=5.0)

                assert created and created.get("id"), "the generate's matter-save did not persist"
                # The headline assertion: the save completes well under a second even
                # under a live verifier storm. A pre-fix lock-starved save took
                # multi-second to 147s; with the persist-point yield it is sub-second.
                assert elapsed < 1.0, (
                    f"GENERATE STARVED: matter-save took {elapsed:.2f}s under a verifier "
                    "storm -- the review writers did not yield the store lock to the "
                    "foreground generate's save."
                )
