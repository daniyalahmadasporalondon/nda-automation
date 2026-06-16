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

This module is the minimal structural guard that makes generate robust REGARDLESS
of background load, without a separate worker process. It exposes:

* :func:`generation_in_progress` -- a context manager the foreground generate
  wraps itself in. While any generate is in flight a process-wide counter is
  non-zero (and an :class:`threading.Event` is clear).
* :func:`active_generation_count` / :func:`generation_active` -- cheap reads.
* :func:`yield_to_active_generation` -- a CPU-bound background unit calls this
  *before* it starts each heavy unit of work. If a generate is in flight it
  blocks (releasing the GIL via ``Event.wait``) until the generate completes or a
  short bound elapses, so the generate's CPU section runs first.

The guarantee: a foreground generate never waits behind more than the ONE
background heavy unit already in progress when it arrives -- never behind the
whole backlog. The gate is fail-open and import-light: any unexpected error in
the background-yield path is swallowed so a guard bug can never wedge a review.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

# Max seconds a background heavy unit will defer to in-flight generation before
# proceeding anyway. This bounds the worst-case background stall (so a stuck/long
# generate can never permanently starve the review pool) while being far longer
# than a deterministic generate needs (~17-58 ms, plus persistence). Env-tunable.
_YIELD_TIMEOUT_ENV = "NDA_GENERATION_PRIORITY_YIELD_TIMEOUT_SECONDS"
_DEFAULT_YIELD_TIMEOUT_SECONDS = 5.0

_LOCK = threading.Lock()
_active_count = 0
# Set (=True) when NO generation is in flight; cleared while a generate runs.
# Background units ``wait()`` on this, which releases the GIL while parked.
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
def generation_in_progress() -> Iterator[None]:
    """Mark a foreground generation as in flight for its duration.

    Increments a process-wide counter and clears the idle event on entry, and
    restores both on exit (even on error). Re-entrant across threads via the
    counter: N concurrent generates keep the gate closed until the last one
    leaves. Wrapping the foreground generate in this is what lets background
    CPU-bound units defer to it.
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


def generation_active() -> bool:
    """True while at least one foreground generation is in flight."""
    return active_generation_count() > 0


def yield_to_active_generation(timeout: float | None = None) -> bool:
    """Defer a background heavy unit while a foreground generation is in flight.

    Call this at the TOP of each CPU-bound background unit of work (one inbound
    AI review, one poller per-message heavy step). If a generate is running it
    parks on the idle event -- which releases the GIL, so the foreground generate
    gets the CPU -- until the generate finishes or ``timeout`` elapses, whichever
    comes first. Returns ``True`` if the path is now clear (idle), ``False`` if it
    proceeded after the timeout while a generate was still running.

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
