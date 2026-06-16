from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable
from types import ModuleType
from typing import Any

from .checker import ParagraphAlignmentError
from .document_limits import ensure_document_size
from .docx_text import DocxExtractionError, detect_docx_tracked_changes, extract_docx_paragraphs
from .matter_lifecycle import BackgroundRunner, RepositoryMatterLifecycle, run_in_daemon_thread
from .matter_repository import DiskMatterRepository, MatterRepository
from .pdf_text import PdfExtractionError, extract_pdf_document
from .review_engine import PlaybookRuntimeFn, ReviewEngineFn, review_nda_with_active_engine
from .review_result_contract import (
    attach_document_source,
    extracted_text_from_paragraphs,
    review_result_paragraphs,
)
from .triage import triage_review_result


LOGGER = logging.getLogger(__name__)

SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".pdf"}

# --------------------------------------------------------------------------- #
# Inbound auto-review (async + bounded worker pool)
# --------------------------------------------------------------------------- #
# Inbound Gmail NDAs import FAST: create_matter_from_document runs only the
# offline deterministic first-pass (defer_ai_review=True) so the single poll
# thread never blocks on the slow Opus/Pro AI review. To restore the core
# feature (inbound NDAs auto-reviewed by AI) WITHOUT the storm that motivated
# bb62b8f, the full active-engine review (assessor + verifier) is run AFTER
# import, OFF the poll thread, by a SINGLE persistent background WORKER POOL
# draining a process-wide queue.
#
# Why a queue + a fixed pool and NOT one daemon thread per matter: a catch-up
# poll can import up to MAX_GMAIL_IMPORT_LIMIT matters in one cycle (default 20,
# overridable via NDA_GMAIL_IMPORT_LIMIT). Spawning a thread per matter parks one
# daemon thread per imported matter blocked on a limit-1 bound -- a transient
# hundreds-of-MB spike, the one remaining OOM scenario on the 2 GB worker. Instead
# a batch enqueues that many CHEAP items (just matter_id/owner) and a FIXED pool of
# inbound_review_concurrency() workers
# (default 1) drains them serially. The pool size IS the concurrency bound, so
# at most N reviews run at once -- never N threads, never N-at-once on the
# worker, never blocking generation/requests, never re-storming the inbox.
INBOUND_REVIEW_CONCURRENCY_ENV = "NDA_INBOUND_REVIEW_CONCURRENCY"
_DEFAULT_INBOUND_REVIEW_CONCURRENCY = 1

# Kill-switch (parallel to NDA_GENERATION_AI_ENABLED): default-ENABLED. Set
# NDA_INBOUND_AI_REVIEW_ENABLED=false (or 0/no/off) to emergency-disable inbound
# auto-review entirely -- imported NDAs keep their deterministic first-pass and
# stay reviewable on-demand, no code change or redeploy required.
INBOUND_AI_REVIEW_ENABLED_ENV = "NDA_INBOUND_AI_REVIEW_ENABLED"

# Hard cap on the queue depth so a runaway producer (a pathological catch-up
# backlog, a buggy sweep) can never grow the queue unboundedly. Comfortably covers
# several MAX_GMAIL_IMPORT_LIMIT batches; the dedup-on-enqueue set means re-enqueues
# of an already-pending matter are free, so this bound is rarely approached.
_INBOUND_REVIEW_QUEUE_MAXSIZE = 256

# Per-sweep cap: the recovery sweep enqueues at most this many un-reviewed
# matters per call so a large historical backlog drains over several poll
# cycles instead of flooding the queue at once.
_INBOUND_REVIEW_SWEEP_LIMIT = 50


def inbound_review_concurrency() -> int:
    """How many inbound auto-reviews may run concurrently (env-configurable).

    Defaults to 1 -- strict serialization, the structural anti-storm guarantee.
    This is the SIZE of the persistent background worker pool, so it bounds both
    concurrency AND the number of live review threads. A larger value (e.g.
    ``NDA_INBOUND_REVIEW_CONCURRENCY=2``) is allowed if a bigger worker can
    absorb it; anything below 1 (or unparseable) clamps to 1 so the pool is
    always a valid bound.
    """

    raw = os.environ.get(INBOUND_REVIEW_CONCURRENCY_ENV, "").strip()
    if not raw:
        return _DEFAULT_INBOUND_REVIEW_CONCURRENCY
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_INBOUND_REVIEW_CONCURRENCY
    return max(1, value)


def inbound_ai_review_enabled() -> bool:
    """Whether inbound auto-review runs at all (env kill-switch).

    Default-ENABLED: returns ``True`` unless ``NDA_INBOUND_AI_REVIEW_ENABLED`` is
    explicitly set to a falsey value (``false``/``0``/``no``/``off``,
    case-insensitive). When ``False`` the scheduler and the recovery sweep are
    both no-ops -- inbound NDAs keep their deterministic first-pass and remain
    reviewable on-demand.
    """

    raw = os.environ.get(INBOUND_AI_REVIEW_ENABLED_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in {"false", "0", "no", "off"}


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


def _perform_inbound_ai_review(
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str,
    review_engine_func: ReviewEngineFn,
    use_semaphore: bool = True,
) -> None:
    """Run the full active-engine review for one inbound matter and persist it.

    Re-reads the matter fresh from durable storage so a matter already reviewed
    (idempotency) is skipped, and so a worker that restarted mid-batch resumes
    only the not-yet-reviewed matters. Fail-soft: any error is logged and
    swallowed -- a failed background review must never crash the worker or wedge
    the poll; the matter keeps its deterministic first-pass and stays reviewable
    on-demand.

    Serialization: when ``use_semaphore`` is True (the explicit-runner path, e.g.
    tests that spawn raw threads) the whole review is gated by the process-wide
    BoundedSemaphore so at most ``inbound_review_concurrency()`` run at once. The
    production worker-pool path passes ``use_semaphore=False`` because the fixed
    pool size is ALREADY the concurrency bound -- gating again would double-gate
    the same review confusingly.
    """

    from contextlib import nullcontext

    gate = _INBOUND_REVIEW_SEMAPHORE if use_semaphore else nullcontext()
    with gate:
        _perform_inbound_ai_review_locked(
            matter_id,
            repository=repository,
            owner_user_id=owner_user_id,
            review_engine_func=review_engine_func,
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
) -> None:
    """The review body, assuming the concurrency bound is already held."""

    from . import telemetry

    try:
        _perform_inbound_ai_review_body(
            matter_id,
            repository=repository,
            owner_user_id=owner_user_id,
            review_engine_func=review_engine_func,
            telemetry=telemetry,
        )
    finally:
        # Sample peak RSS AFTER the review's allocations, whether it succeeded or
        # failed, so even an OOM-adjacent failing review is measured.
        _log_inbound_review_memory(matter_id)


def _perform_inbound_ai_review_body(
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str,
    review_engine_func: ReviewEngineFn,
    telemetry: ModuleType,
) -> None:
    """The actual review work; split out so the locked wrapper can time its memory."""

    try:
        matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
        if not isinstance(matter, dict):
            return
        if _matter_already_ai_reviewed(matter):
            telemetry.increment("inbound_ai_review_skipped_already_reviewed")
            return
        extracted_text = str(matter.get("extracted_text") or "")
        if not extracted_text.strip():
            return
        paragraphs = review_result_paragraphs(matter.get("review_result"))
        review_result = review_engine_func(extracted_text, paragraphs=paragraphs)
    except ParagraphAlignmentError:
        # Stored paragraph offsets did not align; retry text-only so a tracked
        # -changes / reconstructed source still gets its AI review.
        try:
            review_result = review_engine_func(extracted_text)
        except Exception:
            telemetry.increment("inbound_ai_review_failed")
            LOGGER.warning("Inbound AI review failed for matter %s", matter_id, exc_info=True)
            return
    except Exception:
        telemetry.increment("inbound_ai_review_failed")
        LOGGER.warning("Inbound AI review failed for matter %s", matter_id, exc_info=True)
        return

    try:
        updated = repository.update_matter_review(
            matter_id,
            review_result,
            triage_review_result(review_result),
            owner_user_id=owner_user_id,
        )
    except Exception:
        telemetry.increment("inbound_ai_review_failed")
        LOGGER.warning("Inbound AI review persist failed for matter %s", matter_id, exc_info=True)
        return
    if updated is None:
        return
    telemetry.increment("inbound_ai_review_completed")


def _inbound_review_pool_handler(matter_id: str, owner_user_id: str) -> None:
    """Worker-pool per-job handler: review one matter via the default disk repo.

    The production pool stores only cheap ``(matter_id, owner_user_id)`` jobs, so
    the handler reconstructs the default repository + active engine here. No
    semaphore: the pool's fixed size is the concurrency bound.
    """

    _perform_inbound_ai_review(
        matter_id,
        repository=DiskMatterRepository(),
        owner_user_id=str(owner_user_id or ""),
        review_engine_func=review_nda_with_active_engine,
        use_semaphore=False,
    )


_INBOUND_REVIEW_POOL.configure(_inbound_review_pool_handler)


def schedule_inbound_ai_review(
    matter: dict[str, Any] | None,
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
    runner: BackgroundRunner | None = None,
    review_engine_func: ReviewEngineFn | None = None,
) -> bool:
    """Schedule the full ai_first review of a just-imported inbound matter.

    Production path (``runner`` left as ``None``): the matter is ENQUEUED onto a
    process-wide bounded queue drained by a FIXED pool of
    ``inbound_review_concurrency()`` persistent daemon workers. A 100-matter burst
    therefore enqueues 100 cheap items and ONE worker processes them serially --
    never 100 parked threads (the OOM edge this hardening closes).

    Explicit-runner path (a ``runner`` is passed, e.g. tests): the legacy
    per-call behaviour is preserved -- the work runs via ``runner`` and is gated
    by the process-wide semaphore so the serialization/idempotency/fail-soft
    contracts still hold for an injected repository + engine.

    Returns True when work was scheduled, False when there is nothing to do (the
    kill-switch is off, no matter id, a gmail duplicate, or already AI-reviewed).
    Best-effort: a scheduling failure is logged and swallowed so it can never
    break the import that triggered it.
    """

    if not inbound_ai_review_enabled():
        # Emergency kill-switch: keep the deterministic first-pass, no AI review.
        return False
    if not isinstance(matter, dict) or matter.get("_existing_gmail_duplicate"):
        return False
    matter_id = str(matter.get("id") or "")
    if not matter_id:
        return False
    if _matter_already_ai_reviewed(matter):
        return False

    # Production: enqueue onto the persistent worker pool. The pool is the
    # serialization (its fixed size == concurrency bound), so a burst can never
    # spawn one thread per matter.
    if runner is None and repository is None and review_engine_func is None:
        try:
            return _INBOUND_REVIEW_POOL.enqueue(matter_id, str(owner_user_id or ""))
        except Exception:
            from . import telemetry

            telemetry.increment("inbound_ai_review_schedule_failed")
            LOGGER.warning(
                "Failed to enqueue inbound AI review for matter %s", matter_id, exc_info=True
            )
            return False

    # Explicit-runner path (injected repo/engine/runner): legacy semaphore-gated
    # per-call behaviour, preserved for tests and bespoke callers.
    repo = repository or DiskMatterRepository()
    engine = review_engine_func or review_nda_with_active_engine
    run = runner or run_in_daemon_thread

    def _work() -> None:
        _perform_inbound_ai_review(
            matter_id,
            repository=repo,
            owner_user_id=str(owner_user_id or ""),
            review_engine_func=engine,
        )

    try:
        run(_work)
    except Exception:
        from . import telemetry

        telemetry.increment("inbound_ai_review_schedule_failed")
        LOGGER.warning("Failed to schedule inbound AI review for matter %s", matter_id, exc_info=True)
        return False
    return True


def _matter_is_inbound(matter: dict[str, Any]) -> bool:
    """True for an imported INBOUND NDA (gmail / manual upload), not outbound.

    Outbound matters (generated NDAs, send-document deliveries) are reviewed
    on-demand and must not be swept into the inbound auto-review backlog.
    """

    source_type = str(matter.get("source_type") or "").lower()
    if source_type in {"generated", "send_document"}:
        return False
    return True


def recover_unreviewed_inbound_matters(
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
    limit: int = _INBOUND_REVIEW_SWEEP_LIMIT,
) -> int:
    """Re-enqueue inbound matters that never got their AI review (recovery sweep).

    If a worker restarts/OOMs/deploys mid-batch or a transient AI failure occurs,
    the affected NDAs keep ``executed_engine != "ai_first"`` forever -- the next
    poll dedups them and never re-schedules, so they would stay deterministic-only
    silently. This sweep, run on startup AND/OR each poll cycle, finds those
    matters and enqueues them. Idempotent: the ``_matter_already_ai_reviewed``
    guard inside the worker means an already-reviewed matter that slips in is a
    cheap no-op. Bounded: at most ``limit`` matters per sweep, so a large
    historical backlog drains over several cycles instead of flooding the queue.

    Returns the count enqueued. Honours the kill-switch (no-op when disabled) and
    is fully fail-soft.
    """

    if not inbound_ai_review_enabled():
        return 0
    repo = repository or DiskMatterRepository()
    try:
        matters = repo.list_matters(owner_user_id=owner_user_id)
    except Exception:
        LOGGER.warning("Inbound AI review recovery sweep failed to list matters", exc_info=True)
        return 0

    enqueued = 0
    for matter in matters:
        if enqueued >= max(0, int(limit)):
            break
        if not isinstance(matter, dict):
            continue
        if matter.get("_existing_gmail_duplicate"):
            continue
        if not _matter_is_inbound(matter):
            continue
        if _matter_already_ai_reviewed(matter):
            continue
        if not str(matter.get("extracted_text") or "").strip():
            continue
        matter_id = str(matter.get("id") or "")
        if not matter_id:
            continue
        matter_owner = str(matter.get("owner_user_id") or owner_user_id or "")
        try:
            if _INBOUND_REVIEW_POOL.enqueue(matter_id, matter_owner):
                enqueued += 1
        except Exception:
            LOGGER.warning(
                "Inbound AI review recovery sweep failed to enqueue matter %s",
                matter_id,
                exc_info=True,
            )
            continue
    if enqueued:
        from . import telemetry

        telemetry.increment("inbound_ai_review_recovery_enqueued", amount=enqueued)
    return enqueued


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
    defer_ai_review: bool = False,
    drive_sync_runner: BackgroundRunner = run_in_daemon_thread,
    playbook_runtime_func: PlaybookRuntimeFn | None = None,
) -> dict[str, Any]:
    repository = repository or DiskMatterRepository()
    ensure_document_size(document_bytes)
    document_type, extracted_paragraphs, extraction_quality = extract_document(filename, document_bytes)
    extracted_text = extracted_text_from_paragraphs(extracted_paragraphs)
    # Outbound NDA generation defers the slow AI review: it runs the fast
    # deterministic review at creation (so the matter is valid + sendable
    # immediately) and leaves the AI review for on-demand (Refresh Review). Inbound
    # intake (gmail / manual upload) leaves this off and keeps the active engine.
    review_result = review_nda_with_active_engine(
        extracted_text,
        paragraphs=extracted_paragraphs,
        force_engine="deterministic" if defer_ai_review else None,
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
        triage=triage_review_result(review_result),
        source_type=source_type,
        board_column=board_column,
        intake_metadata=intake_metadata,
        dedupe_gmail=dedupe_gmail,
        owner_user_id=owner_user_id,
    )
    RepositoryMatterLifecycle(repository).complete_intake(
        matter,
        owner_user_id=owner_user_id,
        drive_sync_runner=drive_sync_runner,
    )
    return matter


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
    "extract_document",
    "extract_document_paragraphs",
    "inbound_ai_review_enabled",
    "inbound_review_concurrency",
    "is_supported_document_filename",
    "recover_unreviewed_inbound_matters",
    "schedule_inbound_ai_review",
]
