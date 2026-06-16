"""Foreground-generation priority gate (anti-starvation for the generate path).

The single prod worker (``WEB_CONCURRENCY=1``, 1 CPU) runs the user-facing,
*deterministic* NDA generation on the SAME process / CPU / GIL as two heavy
CPU-bound background producers:

* the Gmail poller thread (per-message PDF/DOCX extraction + AI selector/intake);
* the inbound AI-review worker pool (assessor + verifier per imported matter).

Generation is fast (~17-58 ms, no AI call). But when a background producer is
mid-burst it saturates the GIL, so a foreground generate is scheduled in tiny
slices behind a multi-second background CPU section and can blow past the
frontend's 45 s timeout. Generation is never *broken* -- it is *starved*.

This module is the minimal structural guard that gives generation the right of
way REGARDLESS of background load, without a separate worker process. The
contract is intentionally tiny and stable because OTHER modules import it:

* :func:`generation_in_progress` -- ``True`` while any foreground generate is in
  flight (a cheap, lock-guarded read).
* :func:`generation_in_progress_guard` -- the context manager the foreground
  generate wraps itself in; increments/decrements a process-wide in-flight
  counter (so N concurrent generates keep the gate closed until the last one
  leaves) and always clears in ``finally``.
* :func:`should_defer_background_ai` -- the predicate a CPU-bound background
  unit checks BEFORE it starts heavy work. ``True`` => do NOT start now (a
  generate has the right of way); the caller should requeue/skip rather than
  block-wait. Fail-OPEN: any unexpected error returns ``False`` so a guard bug
  can never wedge the review pipeline.

Legacy yield helpers (:func:`yield_to_active_generation`,
:func:`active_generation_count`, :func:`generation_active`) are retained for the
Gmail poller's soft-yield path and existing callers.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

# Max seconds a background heavy unit will defer to in-flight generation before
# proceeding anyway (legacy soft-yield path). This bounds the worst-case
# background stall while being far longer than a deterministic generate needs
# (~17-58 ms, plus persistence). Env-tunable.
_YIELD_TIMEOUT_ENV = "NDA_GENERATION_PRIORITY_YIELD_TIMEOUT_SECONDS"
_DEFAULT_YIELD_TIMEOUT_SECONDS = 5.0

class ForegroundGenerationDeferred(Exception):
    """A background AI unit refused to run because a foreground generate has the
    right of way.

    Raised by :func:`raise_if_generation_active` so a caller that prefers an
    exception-driven control flow (over a boolean check) can ``try/except`` it and
    requeue/skip. Carries the :func:`generation_defer_payload` so a handler can log
    or surface a consistent, telemetry-friendly reason without re-deriving it.
    """

    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or generation_defer_payload()
        super().__init__(str(self.payload.get("reason", "foreground generation in progress")))


_LOCK = threading.Lock()
_active_count = 0
# Set (=True) when NO generation is in flight; cleared while a generate runs.
# Background units may ``wait()`` on this, which releases the GIL while parked.
_idle_event = threading.Event()
_idle_event.set()


def _yield_timeout_seconds() -> float:
    raw = os.environ.get(_YIELD_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_YIELD_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_YIELD_TIMEOUT_SECONDS
    # A non-positive value disables the wait (background never defers); clamp the
    # rest to a small floor so a fat-fingered tiny value still parks meaningfully.
    if value <= 0:
        return 0.0
    return value


@contextmanager
def generation_in_progress_guard() -> Iterator[None]:
    """Mark a foreground generation as in flight for its duration.

    Increments a process-wide counter and clears the idle event on entry, and
    restores both on exit (even on error). Re-entrant across threads via the
    counter: N concurrent generates keep the gate closed until the last one
    leaves. Wrapping the foreground generate in this is what gives it the right
    of way -- background CPU-bound units see :func:`generation_in_progress` go
    ``True`` and defer.
    """

    global _active_count
    with _LOCK:
        _active_count += 1
        if _active_count == 1:
            _idle_event.clear()
    try:
        yield
    finally:
        with _LOCK:
            _active_count -= 1
            if _active_count <= 0:
                _active_count = 0
                _idle_event.set()


def active_generation_count() -> int:
    """How many foreground generations are currently in flight (cheap read)."""
    with _LOCK:
        return _active_count


def generation_in_progress() -> bool:
    """True while at least one foreground generation is in flight (cheap read).

    The public predicate of the shared contract: background CPU-bound units read
    this (via :func:`should_defer_background_ai`) to know a generate has the right
    of way. Use :func:`generation_in_progress_guard` to MARK a generate in flight.
    """
    return active_generation_count() > 0


def generation_active() -> bool:
    """True while at least one foreground generation is in flight (alias)."""
    return active_generation_count() > 0


def should_defer_background_ai() -> bool:
    """Whether a CPU-bound background AI unit should DEFER (not start) right now.

    Returns ``True`` when background AI review must NOT begin -- currently, while
    any foreground NDA generation is in flight, so the user-facing deterministic
    generate keeps the single worker's GIL/CPU and stays under the 45 s frontend
    timeout. The caller's contract is to "refuse to start": requeue the item with
    a small backoff or skip this drain cycle, NEVER drop it.

    Fail-OPEN: any unexpected error returns ``False`` (do not defer) so a bug in
    this guard can never wedge the inbound-review pipeline.
    """

    try:
        return generation_in_progress()
    except Exception:  # pragma: no cover - a guard bug must never wedge a worker.
        return False


def generation_defer_payload() -> dict[str, object]:
    """A small, consistent dict describing the current right-of-way deferral.

    Returned to callers (e.g. the AI verifier, an explicit-refresh handler) that
    skip/queue their work because a foreground generate is in flight, so they can
    log/surface a uniform, telemetry-friendly reason without re-deriving it.
    Fail-open: never raises; reports ``active=0`` on an unexpected error.
    """

    try:
        active = active_generation_count()
    except Exception:  # pragma: no cover - a guard bug must never wedge a worker.
        active = 0
    return {
        "deferred": True,
        "reason": "foreground generation in progress",
        "active_generations": active,
    }


def raise_if_generation_active() -> None:
    """Raise :class:`ForegroundGenerationDeferred` if a foreground generate is in flight.

    The exception-driven twin of :func:`should_defer_background_ai` for callers
    that prefer ``try/except`` control flow. Fail-OPEN: if the liveness check
    itself errors it returns normally (does NOT raise), so a guard bug can never
    block a background unit.
    """

    try:
        active = generation_in_progress()
    except Exception:  # pragma: no cover - a guard bug must never wedge a worker.
        return
    if active:
        raise ForegroundGenerationDeferred(generation_defer_payload())


def yield_store_to_generation(timeout: float | None = None) -> bool:
    """Stand a background store-WRITE back while a foreground generation is in flight.

    The right-of-way ``should_defer_background_ai()`` only stops a background AI
    review from STARTING. But a review that was already mid-flight when the user
    clicked Generate finishes its (slow, lock-free) assessor+verifier work and
    then grabs the single global store lock (``matter_store._locked_store``) to
    persist its result -- contending with generation's OWN several store writes
    (create_matter + artifact backfill + timeline appends + the generated-NDA
    artifact). Under a verifier storm a stream of these review writers repeatedly
    beats generation to the non-fair lock, and generation's save can block for
    minutes. THIS is the persist-point yield that closes that gap: a background
    review writer calls it immediately BEFORE its store write, parks on the idle
    event (which releases the GIL so the foreground generate gets the CPU) until
    EVERY in-flight generation has finished its critical path, then proceeds.

    Returns ``True`` when the path is clear (no generation in flight, so the
    write may proceed at once), ``False`` if it proceeded after the timeout while
    a generation was still in flight (so a stuck/long generate can never wedge the
    review pipeline -- the write still lands, just a beat later).

    Bounded + fail-open: ``timeout`` defaults to the env-tunable yield bound, a
    non-positive timeout means "never yield" (returns immediately), and ANY
    unexpected error returns ``True`` (proceed) rather than holding a review's
    result hostage. This is purely a politeness yield: it NEVER drops or fails the
    write, it only lets generation's lock acquisitions go first.
    """

    try:
        if not generation_in_progress():
            return True
        wait_for = _yield_timeout_seconds() if timeout is None else float(timeout)
        if wait_for <= 0:
            return not generation_in_progress()
        # Event.wait releases the GIL while parked and returns True as soon as the
        # event is set (the LAST in-flight generate completed its critical path,
        # incl. all its store writes). On timeout it returns False and the write
        # proceeds anyway so a background review never stalls unboundedly behind a
        # stuck/long generate.
        return _idle_event.wait(timeout=wait_for)
    except Exception:  # pragma: no cover - a guard bug must never wedge a worker.
        return True


def yield_to_active_generation(timeout: float | None = None) -> bool:
    """Defer a background heavy unit while a foreground generation is in flight.

    Legacy soft-yield used by the Gmail poller's per-message step. If a generate
    is running it parks on the idle event -- which releases the GIL, so the
    foreground generate gets the CPU -- until the generate finishes or ``timeout``
    elapses, whichever comes first. Returns ``True`` if the path is now clear
    (idle), ``False`` if it proceeded after the timeout while a generate was still
    running.

    Bounded + fail-open: ``timeout`` defaults to the env-tunable yield bound, and
    any unexpected error returns ``True`` (proceed) rather than wedging the worker.
    A non-positive timeout means "never defer" (returns immediately).
    """

    try:
        if not generation_active():
            return True
        wait_for = _yield_timeout_seconds() if timeout is None else float(timeout)
        if wait_for <= 0:
            return not generation_active()
        # Event.wait releases the GIL while parked and returns True as soon as the
        # event is set (the last in-flight generate completed). On timeout it
        # returns False and we proceed anyway so the background never stalls
        # unboundedly behind a stuck/long generate.
        return _idle_event.wait(timeout=wait_for)
    except Exception:  # pragma: no cover - a guard bug must never wedge a worker.
        return True
