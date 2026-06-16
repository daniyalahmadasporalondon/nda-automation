"""Per-job phase/wait-time observability for the slow non-generate paths.

The foreground *generate* path is instrumented by ``generation_timing`` (owned by
another builder). This module extends the same idea to the OTHER slow paths — the
on-demand / background AI **review** and the document **render/export** — so that
when one of those is slow the logs say exactly which step ate the time (which model
call for review; soffice-convert vs. rasterize for render).

It deliberately reuses ``generation_timing``'s public seam (``new_request_id`` for
the short correlation id, and ``mark_phase`` so a phase also flows onto any bound
generate stopwatch) while emitting its records under a *distinct* ``event`` name
(``review_phase`` / ``render_phase``) so the three timing streams stay greppable
apart. ``generation_timing`` may not exist on every branch yet, so the import is
defensive: a minimal local stand-in keeps the same names and never raises.

Emit style mirrors the app's existing structured-log lines (see
``openrouter_usage.py``'s ``openrouter_usage`` event): a one-line JSON object on
stdout via ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` with
``flush=True``. Records carry ``{"event", "phase", "elapsed_ms", "request_id",
"cumulative"}``.

Everything here is best-effort and FAIL-OPEN: a timing/logging bug must never
change behavior or raise into the path it is observing.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

# Defensive import of the shared generate-path timing seam. The integrator wires
# the real ``generation_timing``; until then (or in a stripped-down test env) the
# local stand-ins below preserve the same call shape so nothing here breaks.
try:  # pragma: no cover - exercised both ways across branches
    from .generation_timing import mark_phase as _generation_mark_phase
    from .generation_timing import new_request_id as _generation_new_request_id
except Exception:  # noqa: BLE001 - any import failure must fall back, never break

    def _generation_new_request_id() -> str:
        return uuid.uuid4().hex[:12]

    def _generation_mark_phase(phase: str, *, since_start: bool = False) -> None:
        # No generate stopwatch is bound in the stand-in case; a true no-op.
        return


def new_request_id() -> str:
    """A short correlation id for one review/render job (delegates to the shared one)."""
    try:
        return _generation_new_request_id()
    except Exception:  # noqa: BLE001 - id generation must never break the job
        return uuid.uuid4().hex[:12]


class PhaseTimer:
    """Accumulate named phases for one job and emit a structured record per phase.

    Construct one per job with the ``event`` name for this path (``review_phase``
    or ``render_phase``) and an optional ``request_id`` (auto-generated when
    omitted). Use :meth:`phase` as a ``with`` block to time a step, or :meth:`mark`
    /``total`` to stamp a cumulative-since-start total. Every record is also
    forwarded to ``generation_timing.mark_phase`` so a phase shows up on any
    generate stopwatch bound to the current flow (a no-op when none is).

    All emission is wrapped so a logging error can never propagate into the job.
    """

    def __init__(self, event: str, request_id: str | None = None) -> None:
        self.event = str(event or "phase")
        self.request_id = request_id or new_request_id()
        self._origin = time.perf_counter()
        self._last = self._origin

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time a ``with`` body as ``name``; emit the record on exit (even on error)."""
        start = time.perf_counter()
        try:
            yield
        finally:
            now = time.perf_counter()
            elapsed_ms = max(0.0, (now - start) * 1000.0)
            self._last = now
            self._emit(name, elapsed_ms, cumulative=False)

    def mark(self, phase: str, *, since_start: bool = False) -> float:
        """Stamp ``phase`` and emit its record; return the elapsed ms.

        ``elapsed_ms`` is the gap since the previous mark unless ``since_start`` is
        set, in which case it is the time since construction (a job total).
        """
        now = time.perf_counter()
        base = self._origin if since_start else self._last
        elapsed_ms = max(0.0, (now - base) * 1000.0)
        self._last = now
        self._emit(phase, elapsed_ms, cumulative=since_start)
        return elapsed_ms

    def total(self, phase: str = "total") -> float:
        """Convenience: stamp the cumulative time since construction as ``phase``."""
        return self.mark(phase, since_start=True)

    def _emit(self, phase: str, elapsed_ms: float, *, cumulative: bool) -> None:
        try:
            record = {
                "event": self.event,
                "request_id": self.request_id,
                "phase": str(phase),
                "elapsed_ms": round(elapsed_ms, 3),
                "cumulative": bool(cumulative),
            }
            print(
                json.dumps(record, sort_keys=True, separators=(",", ":")),
                file=sys.stdout,
                flush=True,
            )
        except Exception:  # noqa: BLE001 - timing logs must never break the job
            pass
        # Also surface the phase on any bound generate stopwatch (no-op when none).
        try:
            _generation_mark_phase(str(phase), since_start=cumulative)
        except TypeError:
            # An older/stand-in mark_phase without the keyword: best-effort retry.
            try:
                _generation_mark_phase(str(phase))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - forwarding must never break the job
            pass


REVIEW_PHASE_EVENT = "review_phase"
RENDER_PHASE_EVENT = "render_phase"
