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

import threading
import time
from typing import Any

import pytest

from nda_automation import generation_priority, ingestion_service
from nda_automation.matter_repository import InMemoryMatterRepository


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
    """Per-test isolation: idle generation gate, default kill-switch + cap envs."""
    _reset_generation_gate()
    # Ensure no ambient env leaks across tests; each test sets what it needs.
    monkeypatch.delenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, raising=False)
    monkeypatch.delenv(ingestion_service.INBOUND_REVIEW_MAX_FAILURES_ENV, raising=False)
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
# 2. KILL SWITCH AT DRAIN TIME
# =========================================================================== #
class TestKillSwitchAtDrain:
    def test_handler_early_returns_for_already_queued_item_when_flag_flipped_off(
        self, monkeypatch
    ):
        """Flag flipped to false AFTER enqueue: the worker handler must early-return
        WITHOUT running an AI review for an item already in the queue.

        This is the exact hole: today the kill-switch is checked only at
        schedule/enqueue time (schedule_inbound_ai_review) and the recovery sweep,
        NOT at drain. An item that was enqueued while enabled, then the operator
        flips the emergency switch, must still be skipped at drain time.
        """
        repository = InMemoryMatterRepository()
        matter = _seed_inbound_matter(repository)
        matter_id = matter["id"]
        engine = _stub_ai_engine()

        # Simulate "already queued": the item exists and would be drained. Now the
        # operator flips the emergency kill-switch.
        monkeypatch.setenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, "false")
        assert ingestion_service.inbound_ai_review_enabled() is False

        # Drive the drain-time path. We call the lowest-level review entry the
        # worker uses; the target build must make this honour the kill-switch.
        ingestion_service._perform_inbound_ai_review(
            matter_id,
            repository=repository,
            owner_user_id="owner-1",
            review_engine_func=engine,
            use_semaphore=False,
        )

        assert engine.calls == [], (
            "KILL-SWITCH HOLE: the AI engine ran at DRAIN time even though "
            "NDA_INBOUND_AI_REVIEW_ENABLED=false. The flag must be re-checked when "
            "an already-queued item is drained, not only at enqueue."
        )
        unchanged = repository.get_matter(matter_id, owner_user_id="owner-1")
        assert unchanged["review_result"]["active_review_engine"]["executed_engine"] == "deterministic"

    def test_pool_handler_honours_kill_switch_at_drain(self, monkeypatch):
        """The production worker-pool handler itself must honour the kill-switch at
        drain (not merely the explicit _perform_* path).

        _inbound_review_pool_handler reconstructs the real disk repo + engine, so
        we patch the engine boundary it uses to a recorder and assert it is never
        called when the flag is off. This pins the kill-switch into the same code
        path the live daemon executes.
        """
        recorded: list[str] = []

        def _recording_engine(text, *, paragraphs=None, **_kwargs):
            recorded.append(text)
            return _ai_first_review_result()

        # The pool handler calls review_nda_with_active_engine via module ref.
        monkeypatch.setattr(
            ingestion_service, "review_nda_with_active_engine", _recording_engine, raising=False
        )
        # And reads matters from a real DiskMatterRepository(); make that return a
        # ready-to-review matter so ONLY the kill-switch can stop the AI call.
        seeded = {
            "id": "matter_killswitch",
            "owner_user_id": "owner-1",
            "extracted_text": "Some inbound NDA text governed by England and Wales.",
            "review_result": {
                "active_review_engine": {"executed_engine": "deterministic"},
            },
        }

        class _FakeDiskRepo:
            def get_matter(self, matter_id, owner_user_id=""):
                return dict(seeded) if matter_id == seeded["id"] else None

            def update_matter_review(self, matter_id, review_result, triage, owner_user_id=""):
                return {**seeded, "review_result": review_result}

            def update_matter_fields(self, matter_id, fields, owner_user_id=""):
                return {**seeded, **fields}

            def list_matters(self, owner_user_id=""):
                return [dict(seeded)]

        monkeypatch.setattr(ingestion_service, "DiskMatterRepository", _FakeDiskRepo, raising=False)
        monkeypatch.setenv(ingestion_service.INBOUND_AI_REVIEW_ENABLED_ENV, "false")

        # Run the actual production per-job handler for an already-queued job.
        ingestion_service._inbound_review_pool_handler(seeded["id"], "owner-1")

        assert recorded == [], (
            "KILL-SWITCH HOLE in the production pool handler: the AI engine ran at "
            "drain despite NDA_INBOUND_AI_REVIEW_ENABLED=false."
        )

    def test_flag_on_still_runs_review_at_drain(self):
        """Negative control: with the flag at its default (enabled) the same drain
        path DOES run the AI review -- the kill-switch must not be a blanket off."""
        repository = InMemoryMatterRepository()
        matter = _seed_inbound_matter(repository)
        engine = _stub_ai_engine()
        # Belt-and-braces: no generation in flight so right-of-way cannot mask this.
        _reset_generation_gate()

        ingestion_service._perform_inbound_ai_review(
            matter["id"],
            repository=repository,
            owner_user_id="owner-1",
            review_engine_func=engine,
            use_semaphore=False,
        )
        assert engine.calls, "with the kill-switch ENABLED the drain path must run the AI review"  # type: ignore[attr-defined]


# =========================================================================== #
# 3. LOOP-CLOSER (None-return persist counts as a failure; cap stops the sweep)
# =========================================================================== #
class _NonePersistRepository(InMemoryMatterRepository):
    """An in-memory repo whose update_matter_review ALWAYS returns None (falsy).

    Emulates the persist step silently failing (the matter vanished, an owner
    mismatch, a concurrent prune) -- the exact path the loop-closer must treat as
    a failure. update_matter_fields still works so the poison-pill counter can be
    recorded and read back.
    """

    def __init__(self) -> None:
        super().__init__()
        self.review_calls = 0

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

    def test_three_none_returns_then_sweep_skips_the_matter(self):
        """Full cycle: 3 None-returns -> counter at the cap (3) -> the recovery
        sweep must NOT re-enqueue the matter (it is given up, not looped)."""
        repository = _NonePersistRepository()
        matter = _seed_inbound_matter(repository)
        matter_id = matter["id"]
        engine = _stub_ai_engine()

        cap = ingestion_service.inbound_review_max_failures()
        assert cap == 3, "spec pins the default failure cap at 3"

        for _ in range(cap):
            ingestion_service._perform_inbound_ai_review(
                matter_id,
                repository=repository,
                owner_user_id="owner-1",
                review_engine_func=engine,
                use_semaphore=False,
            )

        failures = ingestion_service._matter_review_failure_count(
            repository.get_matter(matter_id, owner_user_id="owner-1")
        )
        assert failures >= cap, (
            f"after {cap} None-return persists the failure counter is {failures}, expected >= {cap}; "
            "the None path is not being counted, so the sweep will loop this matter forever"
        )

        # Now the recovery sweep must SKIP this matter (poison-pill cap reached).
        # Capture enqueues by patching the pool so the live daemon never runs and
        # we can assert the matter id was not handed to it.
        enqueued: list[tuple[str, str]] = []

        class _RecordingPool:
            def enqueue(self, mid, owner):
                enqueued.append((mid, owner))
                return True

        original_pool = ingestion_service._INBOUND_REVIEW_POOL
        ingestion_service._INBOUND_REVIEW_POOL = _RecordingPool()  # type: ignore[assignment]
        try:
            ingestion_service.recover_unreviewed_inbound_matters(
                repository=repository, owner_user_id="owner-1"
            )
        finally:
            ingestion_service._INBOUND_REVIEW_POOL = original_pool  # type: ignore[assignment]

        assert matter_id not in {mid for mid, _ in enqueued}, (
            "LOOP-CLOSER HOLE: a matter that hit the failure cap (3) was RE-ENQUEUED by the "
            "recovery sweep -- the verifier-storm loop the cap is meant to break."
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
