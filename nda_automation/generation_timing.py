"""Phase timing + structured logs for the foreground generate path.

Tonight's generate slowness was lost *queued* — invisibly waiting behind a
background CPU burst — so the request looked stuck with no way to see where the
time went. This module makes that time visible.

A :class:`GenerationStopwatch` accumulates named phases for one
``POST /api/generate-nda`` call and emits a structured ``generation_phase`` log
record per phase. Each record carries a short per-request id and the elapsed
milliseconds, so a slow generate's WAIT time (slot acquisition) is no longer
invisible.

Records are emitted as a one-line JSON object on stdout, matching the app's
existing structured-log style (see ``openrouter_usage.py``'s ``openrouter_usage``
event lines): ``{"event": "generation_phase", ...}`` via
``json.dumps(..., sort_keys=True, separators=(",", ":"))`` with ``flush=True``.

Two ways to record a phase:

* The route owns the stopwatch and times the phases it can see at the HTTP
  boundary — ``generate started``, ``waited for slot`` (guard/lock acquisition
  wait), ``response sent`` (total) — via :meth:`GenerationStopwatch.phase` /
  :meth:`GenerationStopwatch.mark`.
* The deeper workflow phases (``playbook loaded``, ``docx built``,
  ``safety gate passed``, ``matter created``, ``source artifact located``,
  ``generated artifact saved``, ``self check completed``) live inside
  ``nda_generation_workflow`` (owned by another
  builder). A process/thread-local *current stopwatch* is exposed via
  :func:`current_stopwatch` / :func:`mark_phase` so that code can record a phase
  with a single import + one call, without this module reaching into it. When no
  stopwatch is active (e.g. a non-HTTP generate, or a unit test), ``mark_phase``
  is a no-op — it never raises.

Timing/logging is best-effort: a stopwatch bug must never break a generate, so
the emit path swallows its own errors.
"""

from __future__ import annotations

import contextvars
import json
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

_EVENT = "generation_phase"

# The stopwatch bound to the in-flight generate on THIS thread/task. The route
# binds it for the request's duration; workflow code reads it via mark_phase().
# A ContextVar (not a plain global) so concurrent generates on different
# threads/tasks never cross-record into each other's timeline.
_CURRENT: contextvars.ContextVar["GenerationStopwatch | None"] = contextvars.ContextVar(
    "nda_generation_stopwatch", default=None
)


def new_request_id() -> str:
    """A short, collision-resistant id for one generate request (for log correlation)."""
    return uuid.uuid4().hex[:12]


class GenerationStopwatch:
    """Accumulates named phases for one generate request and logs each.

    Construct one per request. Call :meth:`mark` (or use :meth:`phase`) at each
    boundary; every call emits a ``generation_phase`` record stamped with the
    request id, the phase name, and the milliseconds elapsed since the previous
    mark (the phase's own duration). The wall-clock origin is fixed at
    construction so ``response sent`` (or any explicit total) reflects the whole
    request.
    """

    def __init__(self, request_id: str | None = None) -> None:
        self.request_id = request_id or new_request_id()
        self._origin = time.perf_counter()
        self._last = self._origin

    def mark(self, phase: str, *, since_start: bool = False) -> float:
        """Record ``phase`` and emit its log record; return its elapsed ms.

        ``elapsed_ms`` is the gap since the previous mark (this phase's own cost).
        Pass ``since_start=True`` for a cumulative total (e.g. ``response sent``),
        which reports milliseconds since the stopwatch was created instead.
        """
        now = time.perf_counter()
        base = self._origin if since_start else self._last
        elapsed_ms = max(0.0, (now - base) * 1000.0)
        self._last = now
        self._emit(phase, elapsed_ms, since_start=since_start)
        return elapsed_ms

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time a ``with`` body as ``name``; emits the record on exit (even on error)."""
        start = time.perf_counter()
        try:
            yield
        finally:
            now = time.perf_counter()
            elapsed_ms = max(0.0, (now - start) * 1000.0)
            self._last = now
            self._emit(name, elapsed_ms, since_start=False)

    def _emit(self, phase: str, elapsed_ms: float, *, since_start: bool) -> None:
        try:
            record = {
                "event": _EVENT,
                "request_id": self.request_id,
                "phase": str(phase),
                "elapsed_ms": round(elapsed_ms, 3),
                "cumulative": bool(since_start),
            }
            print(
                json.dumps(record, sort_keys=True, separators=(",", ":")),
                file=sys.stdout,
                flush=True,
            )
        except Exception:  # noqa: BLE001 - timing logs must never break a generate
            return


@contextmanager
def bind_stopwatch(stopwatch: GenerationStopwatch) -> Iterator[GenerationStopwatch]:
    """Bind ``stopwatch`` as the current one for the body, restoring on exit.

    Lets workflow code (which must not import the route) record its phases via
    :func:`mark_phase` without being handed the stopwatch explicitly.
    """
    token = _CURRENT.set(stopwatch)
    try:
        yield stopwatch
    finally:
        _CURRENT.reset(token)


def current_stopwatch() -> "GenerationStopwatch | None":
    """The stopwatch bound to the in-flight generate on this thread/task, if any."""
    return _CURRENT.get()


def mark_phase(phase: str, *, since_start: bool = False) -> None:
    """Record ``phase`` on the current stopwatch; a no-op when none is bound.

    The one-call seam for workflow code: ``from . import generation_timing`` then
    ``generation_timing.mark_phase("playbook loaded")``. Safe to call from any
    context — if no generate stopwatch is active it does nothing and never raises.
    """
    stopwatch = _CURRENT.get()
    if stopwatch is None:
        return
    try:
        stopwatch.mark(phase, since_start=since_start)
    except Exception:  # noqa: BLE001 - timing must never break a generate
        return
