from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from types import ModuleType
from typing import Any

from . import artifact_service, generation_priority, pdf_ingest_conversion
from .checker import ParagraphAlignmentError
from .document_limits import ensure_document_size
from .docx_text import DocxExtractionError, detect_docx_tracked_changes, extract_docx_paragraphs
from .matter_lifecycle import BackgroundRunner, RepositoryMatterLifecycle, run_in_daemon_thread
from .matter_repository import DiskMatterRepository, MatterRepository
from .pdf_text import PdfExtractionError, extract_pdf_document
from .review_engine import (
    PlaybookRuntimeFn,
    REVIEW_ENGINE_AI_FIRST,
    ReviewEngineFn,
    review_nda_with_active_engine,
)
from .review_result_contract import (
    attach_document_source,
    extracted_text_from_paragraphs,
    review_result_paragraphs,
)
from .triage import triage_review_result


LOGGER = logging.getLogger(__name__)

SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".pdf"}

# --------------------------------------------------------------------------- #
# On-demand AI review (async + bounded worker pool)
# --------------------------------------------------------------------------- #
# Inbound Gmail NDAs import FAST and DELIBERATELY stay "Not Reviewed": they are
# NEVER auto-reviewed (auto-review on import was the Gmail-storm engine and is
# removed entirely). A full ai_first review (assessor + verifier) runs ONLY when a
# human clicks Review -- the on-demand path (POST /api/matters/<id>/review-refresh
# -> enqueue_on_demand_review). That on-demand work runs OFF the request thread on
# the SINGLE persistent background WORKER POOL below, draining a process-wide queue.
#
# Why a queue + a fixed pool and NOT one daemon thread per job: the pool size IS
# the concurrency bound (default 1), so even a burst of on-demand clicks reviews
# serially in the background -- never N-at-once on the 2 GB worker, never blocking
# generation/requests. The pool, its queue, dedup, and right-of-way deferral are
# UNCHANGED; only the inbound auto-enqueue + kill-switch + recovery sweep that fed
# it from the storm path are gone.
INBOUND_REVIEW_CONCURRENCY_ENV = "NDA_INBOUND_REVIEW_CONCURRENCY"
# Default raised 1 -> 2: the synchronous Gmail-storm enqueue is GONE (only
# on-demand, human-clicked Review jobs reach the pool now), so a single slow
# review no longer needs to be the head-of-line block for every other user's
# click. Two workers let two on-demand reviews drain in parallel. Conservative on
# purpose -- 2, not higher: each concurrent review is a full Opus assessor+verifier
# run, so 2 ~= 2x peak memory/cost on the worker. The pool is designed for N
# workers (see _InboundReviewWorkerPool), so 2 is a safe bound, not a hack.
_DEFAULT_INBOUND_REVIEW_CONCURRENCY = 2

# Hard cap on the queue depth so a runaway producer can never grow the queue
# unboundedly. The dedup-on-enqueue set means re-enqueues of an already-pending
# matter are free, so this bound is rarely approached.
_INBOUND_REVIEW_QUEUE_MAXSIZE = 256

# Poison-pill guard: a review that keeps FAILING for one matter records an
# incrementing failure count + a terminal review_status="failed" so it is never
# left silently stuck "in_progress". (Used by the on-demand review body; there is
# no longer a recovery sweep that re-enqueues, so this is purely the terminal
# failure-recording contract that the board/review polls read.)

# Right-of-way backoff: when a foreground NDA generation is in flight the inbound
# review worker REFUSES to start the heavy AI review and re-queues the matter
# after this short delay (off-thread timer, so it lands AFTER the drain releases
# the dedup key). A deterministic generate is ~17-58 ms; this small backoff lets
# it (and any tightly-following generate) clear before the review retries, while
# never dropping the matter. Env-tunable for ops.
INBOUND_REVIEW_DEFER_BACKOFF_ENV = "NDA_INBOUND_REVIEW_DEFER_BACKOFF_SECONDS"
_DEFAULT_INBOUND_REVIEW_DEFER_BACKOFF_SECONDS = 0.25


def inbound_review_defer_backoff_seconds() -> float:
    """Backoff before a generation-deferred review job is re-enqueued.

    Defaults to ``_DEFAULT_INBOUND_REVIEW_DEFER_BACKOFF_SECONDS``; a non-positive
    or unparseable override clamps to 0 (immediate re-enqueue, still off-thread so
    the dedup key has been released).
    """

    raw = os.environ.get(INBOUND_REVIEW_DEFER_BACKOFF_ENV, "").strip()
    if not raw:
        return _DEFAULT_INBOUND_REVIEW_DEFER_BACKOFF_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_INBOUND_REVIEW_DEFER_BACKOFF_SECONDS
    return max(0.0, value)


# Per-job TOTAL wall-clock deadline. The per-socket urlopen timeout
# (NDA_AI_TIMEOUT_SECONDS) is per-OPERATION: a trickle-feeding endpoint that keeps
# the socket barely alive can hold the single logical review slot far past any
# sane budget, starving every other queued review (head-of-line). This deadline
# bounds the TOTAL time ONE job may own a slot. Default 300s, deliberately BELOW
# the 600s read-time in_progress TTL (matter_view.REVIEW_IN_PROGRESS_TTL_SECONDS):
# a job is freed and stamped terminal before the read layer would even start
# painting it ``stalled``. A non-positive override DISABLES the cap (0 = no
# watchdog), for ops escape-hatch parity with the other tunables.
REVIEW_JOB_DEADLINE_ENV = "NDA_REVIEW_JOB_DEADLINE_SECONDS"
_DEFAULT_REVIEW_JOB_DEADLINE_SECONDS = 300.0


def review_job_deadline_seconds() -> float:
    """Total wall-clock budget one review job may own its slot (env-configurable).

    Defaults to ``_DEFAULT_REVIEW_JOB_DEADLINE_SECONDS`` (300s, under the 600s read
    TTL). ``0`` or negative DISABLES the watchdog (unbounded, the prior behaviour);
    an unparseable value falls back to the default.
    """

    raw = os.environ.get(REVIEW_JOB_DEADLINE_ENV, "").strip()
    if not raw:
        return _DEFAULT_REVIEW_JOB_DEADLINE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_REVIEW_JOB_DEADLINE_SECONDS
    return value


def inbound_review_concurrency() -> int:
    """How many on-demand AI reviews may run concurrently (env-configurable).

    Defaults to 2 -- the synchronous storm enqueue is gone, so two on-demand
    reviews draining in parallel stops one slow review from being the head-of-line
    block for every other user's click. This is the SIZE of the persistent
    background worker pool, so it bounds both concurrency AND the number of live
    review threads. A larger value (e.g. ``NDA_INBOUND_REVIEW_CONCURRENCY=3``) is
    allowed if a bigger worker can absorb it (each concurrent review is a full
    assessor+verifier run, so N ~= Nx peak memory/cost); anything below 1 (or
    unparseable) clamps to 1 so the pool is always a valid bound. (Env var name
    kept for ops/config compatibility.)
    """

    raw = os.environ.get(INBOUND_REVIEW_CONCURRENCY_ENV, "").strip()
    if not raw:
        return _DEFAULT_INBOUND_REVIEW_CONCURRENCY
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_INBOUND_REVIEW_CONCURRENCY
    return max(1, value)


# One process-wide semaphore still gates the explicit-runner path (tests inject a
# runner and rely on this bound). The production path uses the worker pool below,
# whose fixed size is the concurrency bound, so the two never double-gate the
# same review: production reviews go through the pool, never the semaphore.
_INBOUND_REVIEW_SEMAPHORE = threading.BoundedSemaphore(inbound_review_concurrency())


class _InboundReviewWorkerPool:
    """A persistent, lazily-started pool draining a bounded queue of review jobs.

    A batch import enqueues cheap ``(matter_id, owner_user_id)`` jobs; a FIXED
    pool of ``inbound_review_concurrency()`` daemon workers processes them. The
    pool size IS the concurrency/thread bound -- a 100-matter burst never parks
    100 threads. Dedup-on-enqueue (a pending-key set) keeps a re-enqueued matter
    from queueing twice, and the bounded queue refuses overflow so the queue can
    never grow unboundedly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue(
            maxsize=_INBOUND_REVIEW_QUEUE_MAXSIZE
        )
        self._pending: set[tuple[str, str]] = set()
        self._workers: list[threading.Thread] = []
        self._handler: Callable[[str, str], None] | None = None

    def configure(self, handler: Callable[[str, str], None]) -> None:
        """Bind the per-job handler (the function that performs one review)."""
        with self._lock:
            self._handler = handler

    def _ensure_workers(self) -> None:
        size = inbound_review_concurrency()
        with self._lock:
            # Drop dead workers (a crashed daemon thread) so the pool self-heals,
            # then top back up to the configured size.
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            while len(self._workers) < size:
                worker = threading.Thread(
                    target=self._drain,
                    name="inbound-ai-review",
                    daemon=True,
                )
                self._workers.append(worker)
                worker.start()

    def enqueue(self, matter_id: str, owner_user_id: str) -> bool:
        """Enqueue one review job (dedup-on-enqueue, bounded). Returns scheduled."""
        key = (str(matter_id or ""), str(owner_user_id or ""))
        if not key[0]:
            return False
        with self._lock:
            if key in self._pending:
                # Already queued -- a duplicate enqueue (re-poll, sweep) is free.
                return True
            self._pending.add(key)
        try:
            self._queue.put_nowait(key)
        except queue.Full:
            with self._lock:
                self._pending.discard(key)
            from . import telemetry

            telemetry.increment("inbound_ai_review_queue_full")
            return False
        self._ensure_workers()
        return True

    def requeue_after_backoff(self, matter_id: str, owner_user_id: str, delay: float) -> None:
        """Re-enqueue one job after ``delay`` seconds, off-thread (right-of-way defer).

        Used when a worker REFUSES to start a review because a foreground generate
        has the right of way: the matter must not be lost, but re-enqueuing inline
        would be a no-op (its dedup key is still pending until this drain's
        ``finally`` releases it). A short daemon timer re-enqueues AFTER that
        release, so the matter retries instead of being dropped. Fully fail-soft:
        a timer/enqueue failure is logged, never raised into the worker.
        """

        key0 = str(matter_id or "")
        if not key0:
            return

        def _resubmit() -> None:
            try:
                self.enqueue(key0, str(owner_user_id or ""))
            except Exception:  # pragma: no cover - re-enqueue is best-effort.
                LOGGER.warning(
                    "Inbound AI review deferred re-enqueue failed for matter %s",
                    key0,
                    exc_info=True,
                )

        try:
            timer = threading.Timer(max(0.0, float(delay)), _resubmit)
            timer.daemon = True
            timer.start()
        except Exception:  # pragma: no cover - fall back to an inline best-effort retry.
            _resubmit()

    def _drain(self) -> None:
        while True:
            key = self._queue.get()
            matter_id, owner_user_id = key
            try:
                handler = self._handler
                if handler is not None:
                    handler(matter_id, owner_user_id)
            except Exception:  # pragma: no cover - handler is already fail-soft.
                LOGGER.warning(
                    "Inbound AI review worker job failed for matter %s",
                    matter_id,
                    exc_info=True,
                )
            finally:
                with self._lock:
                    self._pending.discard(key)
                self._queue.task_done()

    # -- public observability accessors ------------------------------------- #
    def pending_count(self) -> int:
        """How many distinct review jobs are pending (dedup set size).

        Public, fail-safe read for the deployment-status / telemetry surface: a
        rising pending count means inbound reviews are backing up faster than the
        fixed worker pool drains them -- a saturation / OOM-pressure signal.
        """
        with self._lock:
            return len(self._pending)

    def is_pending(self, matter_id: str, owner_user_id: str) -> bool:
        """Read-only: is a job for ``(matter_id, owner_user_id)`` already queued?

        Additive observability accessor used by the on-demand enqueue path to tell a
        FRESH schedule from a dedup'd re-enqueue (the pool's ``enqueue`` returns True
        for both). It only READS the dedup set under the lock -- it never mutates the
        set, the queue, or any guard.
        """
        key = (str(matter_id or ""), str(owner_user_id or ""))
        with self._lock:
            return key in self._pending

    def queue_depth(self) -> int:
        """Approximate depth of the bounded review queue (items awaiting a worker).

        Uses ``Queue.qsize`` (approximate but lock-free and never blocking); on the
        rare platform where ``qsize`` is unsupported, falls back to the pending-set
        size so the surface always has a number. Read-only, never raises.
        """
        try:
            return self._queue.qsize()
        except NotImplementedError:  # pragma: no cover - qsize unsupported on some OSes
            return self.pending_count()

    # -- test helpers ------------------------------------------------------- #
    def _pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _join_for_tests(self, timeout: float | None = None) -> None:
        """Block until the queue drains (test-only convenience)."""
        if timeout is None:
            self._queue.join()
            return
        # Best-effort bounded wait used by tests.
        import time as _time

        deadline = _time.monotonic() + timeout
        while self._queue.unfinished_tasks and _time.monotonic() < deadline:
            _time.sleep(0.005)


_INBOUND_REVIEW_POOL = _InboundReviewWorkerPool()


# --------------------------------------------------------------------------- #
# On-demand (Review-tab "Refresh") async review — REUSES the inbound pool
# --------------------------------------------------------------------------- #
# The user-initiated POST /api/matters/<id>/review-refresh used to run the heavy
# AI pipeline synchronously inside the request (~145-245s, broken pipe). It now
# ENQUEUES onto the SAME storm-hardened ``_INBOUND_REVIEW_POOL`` (bounded
# concurrency, dedup, idempotency, 256-cap queue, recovery sweep) and returns 202
# immediately. The pool guards/worker loop/queue/dedup/recovery sweep are
# untouched; this is purely additive: a separate on-demand registry + an AI-ONLY
# engine choice so on-demand stays fail-closed AI-first (force_engine=ai_first),
# exactly like the Review-tab's synchronous ``_review_tab_ai_only_engine``, while
# inbound jobs keep their existing active-engine semantics.
#
# Why a SEPARATE registry (NOT the pool's ``_pending`` dedup set): the pool's
# dedup set is part of the hardened guards and MUST NOT be repurposed. This set
# only records "this matter's queued job should run AI-ONLY"; membership is
# consulted by the pool handler to pick the engine and cleared after the job
# drains. Reusing the pool's ``enqueue`` keeps a single concurrency bound and a
# single dedup key per matter, so an on-demand enqueue that races an inbound
# enqueue for the same matter still queues only once.
_ON_DEMAND_REVIEW_LOCK = threading.Lock()
_ON_DEMAND_REVIEW_MATTERS: set[tuple[str, str]] = set()

# The read-time staleness window for a stored ``review_status == "in_progress"``
# (the restart/OOM/deploy guard) lives in ``matter_view.REVIEW_IN_PROGRESS_TTL_SECONDS``
# and is applied ON READ by ``matter_view.review_status_fields`` (board + review
# polls) and the review-refresh payload -- never mutating storage on a GET.


def _on_demand_review_key(matter_id: str, owner_user_id: str) -> tuple[str, str]:
    return (str(matter_id or ""), str(owner_user_id or ""))


def _is_on_demand_review(matter_id: str, owner_user_id: str) -> bool:
    """True when this matter's queued job was scheduled via the on-demand path."""
    with _ON_DEMAND_REVIEW_LOCK:
        return _on_demand_review_key(matter_id, owner_user_id) in _ON_DEMAND_REVIEW_MATTERS


def _on_demand_ai_only_engine(text, **kwargs):
    """On-demand review engine: AI is the ONLY reviewer (fail-closed AI-first).

    Pins ``force_engine=ai_first`` so the on-demand path ignores any
    ``deterministic`` active-engine config and always runs the AI reviewer -- the
    same contract the synchronous Review-tab ``_review_tab_ai_only_engine`` used.
    When the AI reviewer cannot run, ``review_nda_with_active_engine`` raises
    ``ActiveReviewEngineError`` (no deterministic verdict produced); the worker
    records it as a failed attempt (``review_status="failed"`` + ``review_error``).
    """
    return review_nda_with_active_engine(text, force_engine=REVIEW_ENGINE_AI_FIRST, **kwargs)


def _stamp_review_status(
    matter_id: str,
    fields: dict[str, Any],
    *,
    repository: MatterRepository,
    owner_user_id: str,
) -> None:
    """Best-effort allowlisted write of review-lifecycle status fields.

    Fully fail-soft: a status-stamp failure is logged and swallowed -- it must never
    crash the worker or wedge the (already-persisted) review. ``update_matter_fields``
    only merges allowlisted keys (review_status/review_started_at/review_error), so a
    stale field can never be written.
    """
    try:
        repository.update_matter_fields(matter_id, fields, owner_user_id=owner_user_id)
    except Exception:  # pragma: no cover - status stamping is best-effort
        LOGGER.warning(
            "Failed to stamp review status for matter %s: %s", matter_id, fields, exc_info=True
        )


def enqueue_on_demand_review(
    matter_id: str,
    owner_user_id: str = "",
    *,
    repository: MatterRepository | None = None,
) -> tuple[bool, bool, bool]:
    """Enqueue the user-initiated (Review-tab Refresh) AI review onto the inbound pool.

    Returns ``(scheduled, already_pending, queue_full)``:

      * ``(True, False, False)``  -- a fresh job was enqueued (202 in_progress).
      * ``(False, True, False)``  -- a job for this matter was already pending; the
        dedup made the re-enqueue a no-op (202, job_scheduled=false).
      * ``(False, False, True)``  -- the bounded queue is full (503, idle).

    The matter is stamped ``review_status="in_progress"`` + ``review_started_at``
    BEFORE the enqueue (best-effort) so the board/review polls immediately reflect
    progress. The job is registered as on-demand so the pool handler runs the
    AI-ONLY engine (fail-closed AI-first). REUSES ``_INBOUND_REVIEW_POOL.enqueue``
    -- no second pool, no change to the pool's concurrency bound, dedup set,
    idempotency guard, queue cap, recovery sweep, or worker loop.

    ``repository`` is used ONLY for the best-effort in_progress stamp (defaults to
    the disk repo); the pool worker always re-resolves the default disk repo when it
    runs the review, exactly as the inbound path does.
    """

    matter_id = str(matter_id or "")
    owner_user_id = str(owner_user_id or "")
    if not matter_id:
        return (False, False, False)

    key = _on_demand_review_key(matter_id, owner_user_id)

    # Dedup pre-check: was a job for this matter already pending in the pool? The
    # pool's enqueue returns True for BOTH a fresh schedule AND an already-pending
    # re-enqueue, so we READ the pool's pending membership first to distinguish them
    # for the 202 job_scheduled flag. (We only READ the pool's pending set.)
    already_pending = _INBOUND_REVIEW_POOL.is_pending(matter_id, owner_user_id)

    # Register as on-demand BEFORE enqueue so the handler reads AI-ONLY even if a
    # worker drains the job between the enqueue and our return.
    with _ON_DEMAND_REVIEW_LOCK:
        _ON_DEMAND_REVIEW_MATTERS.add(key)

    # Best-effort progress stamp (allowlisted fields only).
    _stamp_review_status(
        matter_id,
        {
            "review_status": "in_progress",
            "review_started_at": datetime.now(timezone.utc).isoformat(),
            "review_error": "",
        },
        repository=repository or DiskMatterRepository(),
        owner_user_id=owner_user_id,
    )

    try:
        scheduled = _INBOUND_REVIEW_POOL.enqueue(matter_id, owner_user_id)
    except Exception:
        # Enqueue failed hard: drop the on-demand marker so a later inbound job is
        # not mis-run AI-only, and surface as queue_full (route -> 503).
        if not already_pending:
            with _ON_DEMAND_REVIEW_LOCK:
                _ON_DEMAND_REVIEW_MATTERS.discard(key)
        from . import telemetry

        telemetry.increment("on_demand_ai_review_schedule_failed")
        LOGGER.warning(
            "Failed to enqueue on-demand AI review for matter %s", matter_id, exc_info=True
        )
        return (False, False, True)

    if not scheduled:
        # Queue full: the pool refused the job. Roll back the on-demand marker only
        # when nothing else is pending for this matter (an already-pending inbound
        # job would still legitimately own it).
        if not already_pending:
            with _ON_DEMAND_REVIEW_LOCK:
                _ON_DEMAND_REVIEW_MATTERS.discard(key)
        from . import telemetry

        telemetry.increment("on_demand_ai_review_queue_full")
        return (False, False, True)

    from . import telemetry

    if already_pending:
        telemetry.increment("on_demand_ai_review_dedup")
        return (False, True, False)
    telemetry.increment("on_demand_ai_review_scheduled")
    return (True, False, False)


def _matter_already_ai_reviewed(matter: dict[str, Any]) -> bool:
    """True when the matter already carries a full AI (ai_first) review result.

    The inbound first-pass stamps ``active_review_engine.executed_engine =
    "deterministic"``; a completed async review overwrites ``review_result`` with
    the ai_first engine output (``executed_engine = "ai_first"``). Checking the
    executed engine makes the async review idempotent: a matter already reviewed
    by the AI (in a prior poll, by on-demand Refresh, or before a worker restart
    finished a different matter) is never re-reviewed.
    """

    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return False
    engine = review_result.get("active_review_engine")
    if not isinstance(engine, dict):
        return False
    return str(engine.get("executed_engine") or "") == "ai_first"


def _matter_review_failure_count(matter: dict[str, Any]) -> int:
    """How many times the background AI review has failed for this matter so far."""
    try:
        return max(0, int(matter.get("inbound_review_failures") or 0))
    except (TypeError, ValueError):
        return 0


_DEFAULT_REVIEW_FAILED_MESSAGE = "AI review failed. Try again."


def _record_inbound_review_failure(
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str,
    error: str = _DEFAULT_REVIEW_FAILED_MESSAGE,
) -> None:
    """Persist an incremented per-matter AI-review failure count (poison-pill guard).

    Best-effort: re-reads the matter for the current count, bumps it, and stamps the
    failure time via the allowlisted ``update_matter_fields`` writer. Any failure
    here is logged and swallowed -- failing to RECORD a failure must never crash the
    worker (the matter simply gets one more retry than the cap, not an infinite
    loop). When the count reaches the cap the recovery sweep stops re-enqueuing it.

    ALSO stamps the async-review lifecycle status (``review_status="failed"`` +
    ``review_error``) in the SAME allowlisted write, so the board/review polls report
    a failed review for free -- correct for the inbound path too, not just on-demand.
    """

    try:
        current = repository.get_matter(matter_id, owner_user_id=owner_user_id)
        previous = _matter_review_failure_count(current) if isinstance(current, dict) else 0
        repository.update_matter_fields(
            matter_id,
            {
                "inbound_review_failures": previous + 1,
                "inbound_review_failed_at": datetime.now(timezone.utc).isoformat(),
                "review_status": "failed",
                "review_error": str(error or _DEFAULT_REVIEW_FAILED_MESSAGE),
            },
            owner_user_id=owner_user_id,
        )
    except Exception:  # pragma: no cover - failure-recording is itself best-effort
        LOGGER.warning(
            "Failed to record inbound AI review failure for matter %s", matter_id, exc_info=True
        )


# Grace before a stored ``in_progress`` is treated as orphaned at BOOT. At boot no
# worker pool thread is running yet (the pool starts lazily on the first enqueue,
# which only happens once the server is serving), so any stored ``in_progress`` is
# by definition not being driven by a live worker. The grace is a belt-and-braces
# guard against a clock-skew edge: a review whose start stamp is within the last
# 60s is left untouched, on the vanishingly-unlikely chance its worker survived a
# warm restart and is about to finish. Env override for ops.
INBOUND_REVIEW_RECONCILE_GRACE_ENV = "NDA_REVIEW_RECONCILE_GRACE_SECONDS"
_DEFAULT_INBOUND_REVIEW_RECONCILE_GRACE_SECONDS = 60.0

# The durable status a boot reconcile stamps onto an orphaned (worker died mid-
# flight) review that has NO completed ai_first result. DISTINCT from ``failed`` (a
# real error) and from the read-time ``stalled`` TTL label (a live-but-slow review):
# ``interrupted`` is RECOVERABLE -- the FE renders a calm Retry and NOTHING auto-runs
# it. We PRODUCE it here; the FE consumes it. Re-running is the user's on-demand
# Refresh, never this reconcile.
REVIEW_STATUS_INTERRUPTED = "interrupted"
_INTERRUPTED_REVIEW_MESSAGE = (
    "The previous review was interrupted by a restart. Click Review to run it again."
)


def _review_reconcile_grace_seconds() -> float:
    """Grace window (seconds) before a stored ``in_progress`` is reconciled at boot."""

    raw = os.environ.get(INBOUND_REVIEW_RECONCILE_GRACE_ENV, "").strip()
    if not raw:
        return _DEFAULT_INBOUND_REVIEW_RECONCILE_GRACE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_INBOUND_REVIEW_RECONCILE_GRACE_SECONDS
    return max(0.0, value)


def _review_started_before_grace(started_at: str, grace_seconds: float) -> bool:
    """True when ``started_at`` is missing/unparseable, or older than the grace.

    A missing/garbled start stamp is treated as old (reconcile it): an orphaned
    review with no usable start time is exactly the kind of stuck record we want to
    clear, never leave wedged ``in_progress`` forever.
    """

    if grace_seconds <= 0:
        return True
    raw = str(started_at or "").strip()
    if not raw:
        return True
    try:
        started = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - started).total_seconds()
    return age >= grace_seconds


def reconcile_interrupted_reviews(
    repository: MatterRepository | None = None,
) -> dict[str, int]:
    """Boot-time reconcile of reviews orphaned by a worker/process death (P0).

    A review stamps ``review_status="in_progress"`` BEFORE handing off to the
    background pool. If the process dies mid-flight (deploy / OOM / restart), that
    stamp is the LAST durable state -- the terminal ``completed``/``failed`` stamp
    never lands, and the record sits ``in_progress`` forever (the read-time TTL only
    paints a transient ``stalled`` label; it never heals the stored value). At boot
    no pool worker is running yet, so EVERY stored ``in_progress`` past a short grace
    is by definition orphaned. For each such matter:

      * if it ALREADY carries a full ``ai_first`` result (``_matter_already_ai_reviewed``)
        the review actually finished and only the terminal stamp was lost -> heal it
        to ``review_status="completed"`` (clear ``review_error``);
      * otherwise the review never produced a result -> stamp the DURABLE,
        RECOVERABLE ``review_status="interrupted"`` + a calm ``review_error`` telling
        the user to click Review to run it again.

    HARD INVARIANT -- STORM-IMPOSSIBLE BY CONSTRUCTION: this function performs a PURE
    STATUS RECONCILE. It NEVER enqueues a job, NEVER calls the AI, NEVER touches the
    worker pool or its queue. It is exactly NOT the removed recovery sweep (whose bug
    was re-enqueuing un-recorded matters forever); re-running an interrupted review is
    left entirely to the user's on-demand Refresh. There is no code path from here to
    an AI call, so no startup state can produce a review storm.

    Owner-safe + idempotent: writes go through the allowlisted ``_stamp_review_status``
    with each matter's own ``owner_user_id`` (no cross-tenant write); ``review_started_at``
    is deliberately PRESERVED (not cleared) so the record keeps its provenance and a
    second run finds the now-terminal matter and skips it (it is no longer
    ``in_progress``). Fully fail-soft: a per-matter error is logged and skipped, and a
    repository-level failure is swallowed -- this must never crash boot. Returns a
    small summary ``{"scanned", "interrupted", "completed", "errors"}`` (also logged).
    """

    summary = {"scanned": 0, "interrupted": 0, "completed": 0, "errors": 0}
    repository = repository or DiskMatterRepository()
    grace_seconds = _review_reconcile_grace_seconds()

    from . import telemetry  # noqa: PLC0415 - keep the import light/local.

    try:
        # Empty owner = ALL matters across every tenant (admin-equivalent read). This
        # is a system boot task, not a request, so it must see every orphaned review.
        matters = repository.list_matters("")
    except Exception:  # pragma: no cover - defensive: never crash boot.
        LOGGER.warning("Interrupted-review reconcile could not list matters", exc_info=True)
        telemetry.increment("review_reconcile_list_failed")
        return summary

    if not isinstance(matters, list):
        return summary

    for matter in matters:
        if not isinstance(matter, dict):
            continue
        if str(matter.get("review_status") or "") != "in_progress":
            continue
        if not _review_started_before_grace(
            str(matter.get("review_started_at") or ""), grace_seconds
        ):
            # Within the grace window -- a warm-restart worker may still finish it.
            continue
        summary["scanned"] += 1
        matter_id = str(matter.get("id") or "")
        if not matter_id:
            continue
        owner_user_id = str(matter.get("owner_user_id") or "")
        try:
            if _matter_already_ai_reviewed(matter):
                # The AI review FINISHED; only the terminal stamp was lost. Heal it.
                _stamp_review_status(
                    matter_id,
                    {"review_status": "completed", "review_error": ""},
                    repository=repository,
                    owner_user_id=owner_user_id,
                )
                summary["completed"] += 1
            else:
                # Orphaned with no result -> durable, RECOVERABLE interrupted state.
                # NOTE: review_started_at is intentionally NOT cleared.
                _stamp_review_status(
                    matter_id,
                    {
                        "review_status": REVIEW_STATUS_INTERRUPTED,
                        "review_error": _INTERRUPTED_REVIEW_MESSAGE,
                    },
                    repository=repository,
                    owner_user_id=owner_user_id,
                )
                summary["interrupted"] += 1
        except Exception:  # pragma: no cover - per-matter best-effort.
            summary["errors"] += 1
            LOGGER.warning(
                "Interrupted-review reconcile failed for matter %s", matter_id, exc_info=True
            )

    telemetry.increment("review_reconcile_interrupted", summary["interrupted"])
    telemetry.increment("review_reconcile_completed", summary["completed"])
    LOGGER.info(
        "Interrupted-review reconcile: scanned=%d interrupted=%d completed=%d errors=%d "
        "(pure status reconcile -- no re-enqueue, no AI call)",
        summary["scanned"],
        summary["interrupted"],
        summary["completed"],
        summary["errors"],
    )
    return summary


def _perform_inbound_ai_review(
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str,
    review_engine_func: ReviewEngineFn,
    use_semaphore: bool = True,
    on_defer: Callable[[], None] | None = None,
    is_on_demand: bool = False,
) -> None:
    """Run the full active-engine review for one inbound matter and persist it.

    Re-reads the matter fresh from durable storage. When ``is_on_demand`` is False
    a matter already AI-reviewed is skipped (idempotency for any background re-drive);
    when ``is_on_demand`` is True (a human clicked Review) that skip is BYPASSED so a
    re-review actually RE-RUNS -- otherwise the button would silently do nothing on
    every matter that already carries an ai_first review. Fail-soft: any error is
    logged and swallowed -- a failed review must never crash the worker or wedge the
    poll; the matter keeps its prior state and stays reviewable on-demand.

    Serialization: when ``use_semaphore`` is True (the explicit-runner path, e.g.
    tests that spawn raw threads) the whole review is gated by the process-wide
    BoundedSemaphore so at most ``inbound_review_concurrency()`` run at once. The
    production worker-pool path passes ``use_semaphore=False`` because the fixed
    pool size is ALREADY the concurrency bound -- gating again would double-gate
    the same review confusingly.

    Right of way (DEEPER than the pool handler's pre-check): the defer check runs
    again INSIDE the concurrency bound, just before the heavy AI work. The pool
    handler gates BEFORE the semaphore, but a generate can start in the window
    between that gate and acquiring the slot. Re-checking here closes that window.
    ``on_defer`` (the pool's requeue) is invoked so the item retries off-thread and
    is NEVER dropped; callers without a requeue (tests/explicit runner) pass None
    and fall through -- the persist-point yield still keeps generation's save fast.
    """

    from contextlib import nullcontext

    # No kill-switch here any more: only on-demand (human-clicked Review) jobs ever
    # reach this function, and they must ALWAYS run -- there is nothing to gate.
    gate = _INBOUND_REVIEW_SEMAPHORE if use_semaphore else nullcontext()
    with gate:
        # RIGHT-OF-WAY re-check INSIDE the bound, before any heavy AI work. When a
        # foreground generate is in flight and the caller supplied a requeue, defer
        # + requeue (off-thread) instead of starting -- the review never begins its
        # GIL/CPU burst while a generate is racing to save. Fail-open via the guard.
        if on_defer is not None and generation_priority.should_defer_background_ai():
            from . import telemetry  # noqa: PLC0415 - keep the import light/local.

            telemetry.increment("inbound_ai_review_deferred_for_generation")
            LOGGER.info(
                "Deferring inbound AI review for matter %s at drain (inside bound): a "
                "foreground generation has the right of way; re-queuing after backoff.",
                matter_id,
            )
            try:
                on_defer()
            except Exception:  # pragma: no cover - requeue is best-effort.
                LOGGER.warning(
                    "Inbound AI review deferred re-enqueue failed for matter %s",
                    matter_id,
                    exc_info=True,
                )
            return
        _perform_inbound_ai_review_locked(
            matter_id,
            repository=repository,
            owner_user_id=owner_user_id,
            review_engine_func=review_engine_func,
            is_on_demand=is_on_demand,
        )


def _log_inbound_review_memory(matter_id: str) -> None:
    """Emit one structured peak-RSS / headroom line for an inbound review.

    The OOM firefight's "measure don't guess" probe at the per-review grain: log
    the worker's current RSS (the peak after this review's allocations) and the
    remaining container headroom, and mirror the same numbers into telemetry gauges
    so they surface in the snapshot. Fully fail-safe -- a probe failure logs nothing
    and never touches the review's own success/failure path.
    """

    from . import process_memory, telemetry

    try:
        rss = process_memory.current_rss_bytes()
        limit = process_memory.container_memory_limit_bytes()
        if rss is None:
            return
        peak_rss_mb = rss / (1024 * 1024)
        headroom_mb: float | None = None
        if limit is not None and limit > 0:
            headroom_mb = (limit - rss) / (1024 * 1024)
        telemetry.set_gauge("inbound_ai_review_last_peak_rss_mb", peak_rss_mb)
        telemetry.gauge_max("inbound_ai_review_max_peak_rss_mb", peak_rss_mb)
        if headroom_mb is not None:
            telemetry.set_gauge("inbound_ai_review_last_headroom_mb", headroom_mb)
        LOGGER.info(
            "Inbound AI review memory matter=%s peak_rss_mb=%.1f headroom_mb=%s",
            matter_id,
            peak_rss_mb,
            f"{headroom_mb:.1f}" if headroom_mb is not None else "unknown",
        )
    except Exception:  # pragma: no cover - observability must never break a review
        return


def _perform_inbound_ai_review_locked(
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str,
    review_engine_func: ReviewEngineFn,
    is_on_demand: bool = False,
) -> None:
    """The review body, assuming the concurrency bound is already held.

    WALL-CLOCK CAP (Task 3): the body is run under a per-job total deadline. The
    per-socket ``urlopen`` timeout is per-operation, so a trickle-feeding endpoint
    could otherwise hold this (single, slot-bounded) thread far past budget and
    starve every other queued review. When ``review_job_deadline_seconds()`` is
    positive the body runs in a daemon sub-thread that this thread ``join``s with
    the deadline; on timeout we RECORD a terminal status and RETURN, freeing the
    logical slot so the next queued job runs immediately.

    RESIDUAL (documented, by design): Python threads cannot be force-killed, so the
    timed-out body keeps running in its detached daemon thread until its own socket
    timeout (``NDA_AI_TIMEOUT_SECONDS``) tears the dead call down on its own. We do
    NOT block on it. Two benign consequences: (1) for a short window two review
    bodies may overlap on the worker (the deadline trade is "free the slot" vs.
    "wait for a wedged call"); (2) if the orphaned body somehow finishes LATE it may
    persist its (real) result and re-stamp ``completed`` over our ``interrupted`` --
    which is a strictly better user outcome (a finished review) than the stuck slot
    we were escaping. When the deadline is non-positive the watchdog is disabled and
    the body runs inline exactly as before.
    """

    from . import telemetry

    deadline = review_job_deadline_seconds()

    try:
        if deadline <= 0:
            # Watchdog disabled: run inline, prior behaviour.
            _perform_inbound_ai_review_body(
                matter_id,
                repository=repository,
                owner_user_id=owner_user_id,
                review_engine_func=review_engine_func,
                telemetry=telemetry,
                is_on_demand=is_on_demand,
            )
        else:
            _run_review_body_with_deadline(
                matter_id,
                repository=repository,
                owner_user_id=owner_user_id,
                review_engine_func=review_engine_func,
                telemetry=telemetry,
                is_on_demand=is_on_demand,
                deadline=deadline,
            )
    finally:
        # Sample peak RSS AFTER the review's allocations, whether it succeeded or
        # failed, so even an OOM-adjacent failing review is measured.
        _log_inbound_review_memory(matter_id)


def _run_review_body_with_deadline(
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str,
    review_engine_func: ReviewEngineFn,
    telemetry: ModuleType,
    is_on_demand: bool,
    deadline: float,
) -> None:
    """Run the review body in a daemon sub-thread bounded by a wall-clock deadline.

    On a clean finish (within budget) this is transparent -- identical to running
    the body inline. On timeout it stamps a terminal ``interrupted`` status (the
    same RECOVERABLE state a boot reconcile uses: the FE shows a calm Retry, nothing
    auto-runs it) and RETURNS, releasing the slot. The detached body thread lives on
    until its socket timeout; see the caller's RESIDUAL note. Fail-soft: a spawn
    failure falls back to running the body inline (better an unbounded run than a
    dropped review).
    """

    done = threading.Event()

    def _runner() -> None:
        try:
            _perform_inbound_ai_review_body(
                matter_id,
                repository=repository,
                owner_user_id=owner_user_id,
                review_engine_func=review_engine_func,
                telemetry=telemetry,
                is_on_demand=is_on_demand,
            )
        finally:
            done.set()

    try:
        worker = threading.Thread(
            target=_runner,
            name=f"inbound-ai-review-job-{matter_id}",
            daemon=True,
        )
        worker.start()
    except Exception:  # pragma: no cover - thread spawn failure: run inline instead.
        LOGGER.warning(
            "Review-deadline watchdog could not spawn for matter %s; running inline",
            matter_id,
            exc_info=True,
        )
        _perform_inbound_ai_review_body(
            matter_id,
            repository=repository,
            owner_user_id=owner_user_id,
            review_engine_func=review_engine_func,
            telemetry=telemetry,
            is_on_demand=is_on_demand,
        )
        return

    finished = done.wait(timeout=deadline)
    if finished:
        return

    # DEADLINE EXCEEDED: free the slot. Stamp a terminal RECOVERABLE status so the
    # board/review polls stop reporting "in progress" forever and the user gets a
    # calm Retry. The detached body thread keeps running until its socket timeout.
    telemetry.increment("inbound_ai_review_deadline_exceeded")
    LOGGER.warning(
        "Inbound AI review exceeded the %.0fs job deadline for matter %s; freeing the "
        "slot and stamping a recoverable interrupted status (the call dies on its own "
        "socket timeout).",
        deadline,
        matter_id,
    )
    _stamp_review_status(
        matter_id,
        {
            "review_status": REVIEW_STATUS_INTERRUPTED,
            "review_error": (
                "The review took too long and was stopped. Click Review to run it again."
            ),
        },
        repository=repository,
        owner_user_id=owner_user_id,
    )


def _perform_inbound_ai_review_body(
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str,
    review_engine_func: ReviewEngineFn,
    telemetry: ModuleType,
    is_on_demand: bool = False,
) -> None:
    """The actual review work; split out so the locked wrapper can time its memory."""

    try:
        matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
        if not isinstance(matter, dict):
            return
        # Snapshot the matter's updated_at BEFORE the (multi-minute) AI assessor +
        # verifier run. The guarded persist below compares against it: if a human
        # edit (a mark-reviewed human_reviewed=True or a saved redline_draft) lands
        # during that window, updated_at will have moved and those edits are PRESERVED
        # rather than silently reset/popped by the unconditional update_matter_review.
        expected_updated_at = str(matter.get("updated_at") or "")
        # Idempotency skip ONLY for non-on-demand drives. An ON-DEMAND review (a human
        # clicked Review) must BYPASS this -- otherwise the button silently does
        # nothing for every matter that already carries an ai_first review (a
        # re-review after playbook drift / a text change). The route already gates
        # whether a re-review is warranted (staleness/force); once it enqueues, the
        # drain must actually re-run.
        if not is_on_demand and _matter_already_ai_reviewed(matter):
            LOGGER.info(
                "review skip matter=%s origin=inbound reason=already_ai_first_reviewed",
                matter_id,
            )
            telemetry.increment("inbound_ai_review_skipped_already_reviewed")
            return
        extracted_text = str(matter.get("extracted_text") or "")
        if not extracted_text.strip():
            # LOOP CLOSER: no readable text was extracted (scanned / image-only /
            # encrypted PDF). Previously this returned SILENTLY -- the ONE early-return
            # that recorded NOTHING: the matter was never stamped a terminal status
            # (it sat at review_status="in_progress" forever, showing "in review") and
            # the poison-pill counter never advanced, so the matter was never marked
            # ai_first reviewed AND never counted as failed -- the recovery sweep kept
            # finding an un-reviewed, un-failed matter to re-enqueue every cycle, the
            # never-ending review storm that slipped past the 3-strike brake. Treat
            # empty extracted text as a TERMINAL, RECORDED failure, exactly like the
            # other failure early-returns: bump the poison-pill counter (so the cap
            # engages) AND stamp review_status="failed" with a human-readable reason.
            # This is non-retryable (the text will never appear), so the cap stops it.
            telemetry.increment("inbound_ai_review_failed")
            telemetry.increment("inbound_ai_review_empty_extracted_text")
            _record_inbound_review_failure(
                matter_id,
                repository=repository,
                owner_user_id=owner_user_id,
                error=(
                    "No readable text could be extracted from the document "
                    "-- it may be a scanned or image-only PDF."
                ),
            )
            LOGGER.warning(
                "Inbound AI review skipped for matter %s owner=%s: no readable extracted "
                "text (scanned/image-only/encrypted PDF). Recorded as a terminal failure "
                "so the poison-pill cap stops the recovery sweep re-enqueuing it.",
                matter_id,
                str(owner_user_id or ""),
            )
            return
        LOGGER.info(
            "Running inbound AI review for matter %s owner=%s",
            matter_id,
            str(owner_user_id or ""),
        )
        # Approach C: a converted PDF matter carries re-keyed working-DOCX paragraphs
        # (source_index re-stamped to the reconstructed body, source_part:"pdf"
        # dropped). Prefer those so the redlines this review produces anchor by index
        # into the working DOCX exactly like a native-DOCX matter. Fall back to the
        # review_result's stored paragraphs (native DOCX) when no conversion ran.
        working_paragraphs = matter.get(WORKING_DOCX_PARAGRAPHS_FIELD)
        if isinstance(working_paragraphs, list) and working_paragraphs:
            paragraphs = working_paragraphs
        else:
            paragraphs = review_result_paragraphs(matter.get("review_result"))
        review_result = review_engine_func(extracted_text, paragraphs=paragraphs)
    except ParagraphAlignmentError:
        # Stored paragraph offsets did not align; retry text-only so a tracked
        # -changes / reconstructed source still gets its AI review.
        try:
            review_result = review_engine_func(extracted_text)
        except Exception:
            telemetry.increment("inbound_ai_review_failed")
            _record_inbound_review_failure(matter_id, repository=repository, owner_user_id=owner_user_id)
            LOGGER.warning("Inbound AI review failed for matter %s", matter_id, exc_info=True)
            return
    except Exception:
        telemetry.increment("inbound_ai_review_failed")
        _record_inbound_review_failure(matter_id, repository=repository, owner_user_id=owner_user_id)
        LOGGER.warning("Inbound AI review failed for matter %s", matter_id, exc_info=True)
        return

    # PERSIST-POINT RIGHT OF WAY. The drain-time gate (_inbound_review_pool_handler)
    # only stops a review from STARTING. A review that was already mid-flight when a
    # Generate began has finished its slow, lock-free assessor+verifier work by now
    # and is about to grab the single global store lock -- contending with the
    # foreground generate's OWN several store writes. Stand back here so generation's
    # save acquires first. Bounded + fail-open: a stuck/long generate can never wedge
    # this write; it just lands a beat later. NEVER drops or fails the persist.
    generation_priority.yield_store_to_generation()
    try:
        updated = repository.refresh_matter_review(
            matter_id,
            review_result,
            triage_review_result(review_result),
            expected_updated_at=expected_updated_at,
            owner_user_id=owner_user_id,
        )
    except Exception:
        telemetry.increment("inbound_ai_review_failed")
        _record_inbound_review_failure(matter_id, repository=repository, owner_user_id=owner_user_id)
        LOGGER.warning("Inbound AI review persist failed for matter %s", matter_id, exc_info=True)
        return
    if not updated:
        # LOOP CLOSER: the review ran but the persist returned None/falsy (e.g. the
        # matter vanished, an owner mismatch, or a writer no-op). Previously this
        # returned silently WITHOUT recording anything, so the matter was never
        # stamped ai_first AND never counted as failed -> the recovery sweep re-swept
        # it forever (the never-ending review storm). Treat the un-persistable save
        # as a FAILED attempt: bump the poison-pill counter (same path the except
        # branches use) so the 3-strike brake engages and the matter stops being
        # re-reviewed. Name the matter id so these orphaned matters can be found /
        # re-homed later.
        telemetry.increment("inbound_ai_review_failed")
        telemetry.increment("inbound_ai_review_persist_returned_none")
        _record_inbound_review_failure(matter_id, repository=repository, owner_user_id=owner_user_id)
        LOGGER.warning(
            "Inbound AI review persisted nothing for matter %s owner=%s "
            "(refresh_matter_review returned None); counted as a failed attempt so the "
            "poison-pill cap can stop it being re-swept. This matter may be orphaned "
            "(missing/owner-mismatch) and needs re-homing.",
            matter_id,
            str(owner_user_id or ""),
        )
        return
    # PERSIST SUCCESS: stamp the async-review lifecycle status (completed + cleared
    # error) in the SAME terminal write so the board/review polls report a finished
    # review for free -- correct for inbound AND on-demand. Best-effort (fail-soft):
    # a status-stamp failure never undoes the (already-persisted) review.
    _stamp_review_status(
        matter_id,
        {"review_status": "completed", "review_error": ""},
        repository=repository,
        owner_user_id=owner_user_id,
    )
    telemetry.increment("inbound_ai_review_completed")


def _inbound_review_pool_handler(matter_id: str, owner_user_id: str) -> None:
    """Worker-pool per-job handler: review one matter via the default disk repo.

    The pool stores only cheap ``(matter_id, owner_user_id)`` jobs, so the handler
    reconstructs the default repository + engine here. No semaphore: the pool's
    fixed size is the concurrency bound. Only the ON-DEMAND path (a human clicking
    Review) enqueues jobs now -- there is NO inbound auto-enqueue and NO kill-switch
    to skip a queued review, so a clicked Review always runs.

    ONE gate runs BEFORE any heavy AI work:

      RIGHT OF WAY: if a foreground NDA generation is in flight
      (``should_defer_background_ai()``), this worker REFUSES to start the review
      and re-queues the matter after a short backoff instead of block-waiting --
      a hard "don't start now", not a soft yield. The matter is never dropped
      (re-enqueued off-thread once the dedup key releases), so the user-facing
      generate keeps the single worker's GIL/CPU and stays under the frontend's
      45 s timeout.

    Observability: emits structured START / SKIP / COMPLETE / ERROR log lines
    tagged with the matter id, origin, and elapsed seconds, so a stuck/skipped/slow
    review is visible in logs (the drain path was otherwise a black box).
    """

    # On-demand jobs (Review button) run the AI-ONLY engine (fail-closed AI-first).
    # The marker is consulted here and cleared once the job reaches a TERMINAL state
    # (run) -- NOT on a generation defer, where the job is re-queued and must still
    # run AI-only. (A non-on-demand job should not reach the pool any more, but if
    # one ever did it falls back to the active engine.)
    on_demand = _is_on_demand_review(matter_id, owner_user_id)
    origin = "on_demand" if on_demand else "inbound"
    review_engine = _on_demand_ai_only_engine if on_demand else review_nda_with_active_engine

    # Gate: give foreground generation the right of way -- defer + re-queue. The
    # job is re-queued (not dropped), so the on-demand marker is LEFT in place.
    if generation_priority.should_defer_background_ai():
        from . import telemetry  # noqa: PLC0415 - keep the import light/local.

        telemetry.increment("inbound_ai_review_deferred_for_generation")
        LOGGER.info(
            "review skip matter=%s origin=%s reason=generation_right_of_way; "
            "re-queuing after backoff",
            matter_id,
            origin,
        )
        _INBOUND_REVIEW_POOL.requeue_after_backoff(
            matter_id, owner_user_id, inbound_review_defer_backoff_seconds()
        )
        return

    # Track a DEEPER defer (a generate that starts inside the concurrency bound):
    # when that happens the job is re-queued, so the on-demand marker must SURVIVE --
    # only a terminal run clears it.
    deferred = {"flag": False}

    def _on_defer() -> None:
        deferred["flag"] = True
        _INBOUND_REVIEW_POOL.requeue_after_backoff(
            matter_id, owner_user_id, inbound_review_defer_backoff_seconds()
        )

    LOGGER.info("review start matter=%s origin=%s", matter_id, origin)
    started_at = time.monotonic()
    try:
        _perform_inbound_ai_review(
            matter_id,
            repository=DiskMatterRepository(),
            owner_user_id=str(owner_user_id or ""),
            review_engine_func=review_engine,
            use_semaphore=False,
            on_defer=_on_defer,
            is_on_demand=on_demand,
        )
    except Exception:
        # _perform_inbound_ai_review is already fail-soft, but log any escape here so
        # an unexpected error in the drain path is never silent.
        LOGGER.warning(
            "review error matter=%s origin=%s elapsed=%.1fs",
            matter_id,
            origin,
            time.monotonic() - started_at,
            exc_info=True,
        )
        raise
    else:
        if deferred["flag"]:
            LOGGER.info(
                "review deferred matter=%s origin=%s elapsed=%.1fs (re-queued)",
                matter_id,
                origin,
                time.monotonic() - started_at,
            )
        else:
            LOGGER.info(
                "review complete matter=%s origin=%s elapsed=%.1fs",
                matter_id,
                origin,
                time.monotonic() - started_at,
            )
    finally:
        # Terminal: the review ran (completed or failed) on this drain. Clear the
        # on-demand marker UNLESS the job was re-queued by a deeper generation defer
        # (then the re-queued job must still run AI-only).
        if on_demand and not deferred["flag"]:
            with _ON_DEMAND_REVIEW_LOCK:
                _ON_DEMAND_REVIEW_MATTERS.discard(_on_demand_review_key(matter_id, owner_user_id))


_INBOUND_REVIEW_POOL.configure(_inbound_review_pool_handler)


def create_matter_from_document(
    *,
    filename: str,
    document_bytes: bytes,
    source_type: str = "gmail_demo",
    board_column: str = "gmail_demo",
    intake_metadata: dict[str, Any] | None = None,
    dedupe_gmail: bool = False,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
    defer_ai_review: bool = True,
    drive_sync_runner: BackgroundRunner = run_in_daemon_thread,
    playbook_runtime_func: PlaybookRuntimeFn | None = None,
) -> dict[str, Any]:
    repository = repository or DiskMatterRepository()
    ensure_document_size(document_bytes)
    document_type, extracted_paragraphs, extraction_quality = extract_document(filename, document_bytes)
    extracted_text = extracted_text_from_paragraphs(extracted_paragraphs)
    # defer_ai_review DEFAULTS TO TRUE: a matter is created UN-REVIEWED and no review
    # runs at create. This is the storm-safe default -- NO caller can accidentally
    # trigger a synchronous inbound review. Every matter (inbound Gmail, manual
    # upload, generated) stays "Not Reviewed Yet" and the full AI review runs ONLY
    # on-demand when a human clicks Review (enqueue_on_demand_review). A caller may
    # still pass defer_ai_review=False to opt into a synchronous eager review at
    # create, but no production path does (it was the OOM/cost-storm trigger).
    if defer_ai_review:
        review_result = None
    else:
        review_result = review_nda_with_active_engine(
            extracted_text,
            paragraphs=extracted_paragraphs,
            **({"playbook_runtime_func": playbook_runtime_func} if playbook_runtime_func is not None else {}),
        )
        attach_document_source(
            review_result,
            filename=filename,
            document_type=document_type,
            extracted_paragraphs=extracted_paragraphs,
            extracted_text=extracted_text,
            extraction_quality=extraction_quality,
        )
    matter = repository.create_matter(
        source_filename=filename,
        document_bytes=document_bytes,
        extracted_text=extracted_text,
        review_result=review_result,
        triage=triage_review_result(review_result) if review_result is not None else {},
        source_type=source_type,
        board_column=board_column,
        intake_metadata=intake_metadata,
        dedupe_gmail=dedupe_gmail,
        owner_user_id=owner_user_id,
    )
    # Approach C: a PDF source matter is reconstructed to a canonical working DOCX
    # ONCE here at ingest and its review paragraphs re-keyed to anchor by index into
    # that DOCX (so a converted PDF thereafter behaves exactly like a native DOCX
    # matter). FAIL-OPEN: any conversion error leaves the legacy un-converted PDF
    # matter intact -- ingest NEVER hard-blocks on the conversion.
    matter = _convert_pdf_matter_at_ingest(
        matter,
        document_type=document_type,
        document_bytes=document_bytes,
        extracted_paragraphs=extracted_paragraphs,
        repository=repository,
        owner_user_id=owner_user_id,
    )
    RepositoryMatterLifecycle(repository).complete_intake(
        matter,
        owner_user_id=owner_user_id,
        drive_sync_runner=drive_sync_runner,
    )
    return matter


# The matter field that carries the PDF→DOCX-reconstruction body paragraphs, already
# re-keyed (``source_index`` re-stamped, ``source_part:"pdf"`` dropped) to anchor by
# index into the working DOCX. The on-demand review prefers these over the raw pypdf
# paragraphs so the redlines it produces anchor into the working DOCX body.
WORKING_DOCX_PARAGRAPHS_FIELD = "working_docx_paragraphs"


def _convert_pdf_matter_at_ingest(
    matter: dict[str, Any],
    *,
    document_type: str,
    document_bytes: bytes,
    extracted_paragraphs: list[dict[str, Any]],
    repository: MatterRepository,
    owner_user_id: str,
) -> dict[str, Any]:
    """Reconstruct a PDF matter to a working DOCX and persist it (Approach C).

    Only PDF matters are converted; a DOCX matter is returned untouched. On success
    the working DOCX is registered as a role="working" artifact and the re-keyed
    review paragraphs are stored on the matter. FAIL-OPEN: on ANY error the original
    (un-converted) matter is returned and ingest proceeds, so a flaky/unavailable
    reconstruction engine never blocks an import.
    """
    if document_type != "pdf":
        return matter
    matter_id = str(matter.get("id") or "")
    source_filename = str(matter.get("source_filename") or matter.get("stored_filename") or "")
    if not matter_id:
        return matter
    try:
        working = pdf_ingest_conversion.convert_pdf_matter_to_docx(
            document_bytes,
            source_filename,
            extracted_paragraphs,
        )
    except Exception:
        LOGGER.warning(
            "PDF->working-DOCX conversion failed for matter %s; keeping legacy PDF matter",
            matter_id,
            exc_info=True,
        )
        return matter
    # ORDER MATTERS (half-persist guard): the working artifact is what makes the
    # export substitute the working DOCX + light up working_docx_ready, and the
    # re-keyed paragraphs are what make the review produce redlines that anchor into
    # it. Persist the paragraphs FIRST, then register the artifact LAST, so the two
    # are never out of step in the harmful direction: a registered working artifact
    # ALWAYS has its re-keyed paragraphs behind it. If artifact registration fails
    # after the field write, roll the orphan field back so the matter falls cleanly
    # to the legacy un-converted PDF path rather than the divergent (artifact-only /
    # paragraphs-only) state Approach C exists to remove.
    try:
        updated = repository.update_matter_fields(
            matter_id,
            {WORKING_DOCX_PARAGRAPHS_FIELD: working.paragraphs},
            owner_user_id=owner_user_id,
        )
    except Exception:
        LOGGER.warning(
            "Persisting PDF->working-DOCX paragraphs failed for matter %s; keeping legacy PDF matter",
            matter_id,
            exc_info=True,
        )
        return matter
    try:
        artifact_service.register_working_docx(
            matter_id,
            working.docx_bytes,
            repository=repository,
            owner_user_id=owner_user_id,
        )
    except Exception:
        LOGGER.warning(
            "Registering PDF->working-DOCX artifact failed for matter %s; rolling back "
            "the re-keyed paragraphs and keeping legacy PDF matter",
            matter_id,
            exc_info=True,
        )
        try:
            rolled_back = repository.update_matter_fields(
                matter_id,
                {WORKING_DOCX_PARAGRAPHS_FIELD: None},
                owner_user_id=owner_user_id,
            )
        except Exception:
            LOGGER.warning(
                "Rolling back PDF->working-DOCX paragraphs failed for matter %s", matter_id, exc_info=True
            )
            return updated if isinstance(updated, dict) else matter
        return rolled_back if isinstance(rolled_back, dict) else matter
    LOGGER.info(
        "PDF->working-DOCX conversion stored for matter %s (mapped=%d unmapped=%d)",
        matter_id,
        working.mapped_count,
        working.unmapped_count,
    )
    return updated if isinstance(updated, dict) else matter


def extract_document_paragraphs(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]]]:
    document_type, paragraphs, _quality = extract_document(filename, document_bytes)
    return document_type, paragraphs


def extract_document(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]], dict[str, object] | None]:
    lower_filename = filename.lower()
    if lower_filename.endswith(".docx"):
        paragraphs = extract_docx_paragraphs(document_bytes)
        # Surface unresolved tracked changes as an extraction-quality warning so
        # the review never silently acts on a synthesized redline state. The flat
        # text now reflects the in-force baseline (see docx_text), but the matter
        # must still be flagged + gated for human resolution of the redlines.
        tracked_changes = detect_docx_tracked_changes(document_bytes)
        return "docx", paragraphs, tracked_changes
    if lower_filename.endswith(".pdf"):
        extraction = extract_pdf_document(document_bytes)
        return "pdf", extraction.paragraphs, extraction.quality
    raise ValueError("Upload a .docx Word document or text-based PDF.")


def is_supported_document_filename(filename: object) -> bool:
    if not isinstance(filename, str):
        return False
    return any(filename.lower().endswith(extension) for extension in SUPPORTED_DOCUMENT_EXTENSIONS)


__all__ = [
    "DocxExtractionError",
    "ParagraphAlignmentError",
    "PdfExtractionError",
    "create_matter_from_document",
    "enqueue_on_demand_review",
    "extract_document",
    "extract_document_paragraphs",
    "inbound_review_concurrency",
    "is_supported_document_filename",
]
