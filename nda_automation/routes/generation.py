"""Generate an outbound NDA from the draft-ui intake.

POST /api/generate-nda is what the Generator tab's "Generate NDA" button calls.
The endpoint is intentionally thin: HTTP details stay here, while payload
translation, generation, safety checking, persistence, and response shaping live
in ``nda_generation_workflow``.

This route also owns the request-level observability for generate: a per-request
:class:`~nda_automation.generation_timing.GenerationStopwatch` times the phases
visible at the HTTP boundary — ``generate started``, ``waited for slot`` (the
foreground-priority guard acquisition wait), and ``response sent`` (total) — and
binds itself as the *current* stopwatch so the deterministic workflow can record
its own phases (``playbook loaded`` / ``docx built`` / ``matter persisted``) via
``generation_timing.mark_phase`` without depending on this route. The WAIT time
is the headline metric: tonight a slow generate's time was lost queued behind a
background CPU burst, invisibly — now it is a logged phase.
"""

from __future__ import annotations

from contextlib import contextmanager

from .. import nda_generation, nda_generation_workflow, telemetry
from .. import generation_timing
from ..nda_generation import NdaGenerationError
from .common import request_owner_user_id

# Shared contract (owned by ``nda_automation.generation_priority``): a context
# manager that marks a foreground generation in-flight for its body so the
# right-of-way mechanism can see a generate is active. We bind to the contract
# name ``generation_in_progress_guard`` but tolerate the older ``generation_in_progress``
# name, and fall back to a no-op so this route runs standalone until the
# integrator wires the real guard.
try:  # pragma: no cover - import wiring; exercised indirectly by the route tests
    from ..generation_priority import generation_in_progress_guard
except ImportError:  # pragma: no cover - fallback paths are import-shape only
    try:
        from ..generation_priority import (  # type: ignore[no-redef]
            generation_in_progress as generation_in_progress_guard,
        )
    except ImportError:

        @contextmanager
        def generation_in_progress_guard():  # type: ignore[no-redef]
            """No-op fallback used only when the priority module is absent."""
            yield


def handle_generate_nda(handler) -> None:
    telemetry.increment("generate_nda_requests")

    stopwatch = generation_timing.GenerationStopwatch()
    stopwatch.mark("generate started")

    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        # Wrap the whole generate in the foreground-priority guard so the
        # CPU-bound background producers (inbound AI-review worker pool, Gmail
        # poller) defer to it and never starve it of the single worker's GIL/CPU.
        # Acquiring the guard is where a foreground generate can BLOCK behind an
        # in-flight background heavy unit, so we time the acquisition itself as
        # the "waited for slot" phase -- the time tonight's generate spent lost
        # in the queue, now visible.
        with stopwatch.phase("waited for slot"):
            guard = generation_in_progress_guard()
            guard.__enter__()
        try:
            # Bind the stopwatch so the deterministic workflow can record its own
            # phases (playbook loaded / docx built / matter persisted) via
            # generation_timing.mark_phase without importing this route.
            with generation_timing.bind_stopwatch(stopwatch):
                generated = nda_generation_workflow.generate_nda_from_payload(
                    payload,
                    owner_user_id=request_owner_user_id(handler),
                )
        finally:
            guard.__exit__(None, None, None)
    except nda_generation_workflow.GenerationPayloadError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except NdaGenerationError as error:
        # Unknown entity, unapproved governing law, malformed template, an
        # unsupported posture (one-way) — client-correctable input errors — OR the
        # hard safety gate refusing an off-position draft. All surface as 400 with a
        # clear message and NEVER return the document; the safety-gate trip gets its
        # own telemetry signal so a drifting AI is visible in the metrics.
        if str(error).startswith(nda_generation.SAFETY_GATE_MESSAGE):
            telemetry.increment("generate_nda_safety_gate_blocked")
        else:
            telemetry.increment("generate_nda_rejected")
        handler._send_json({"error": str(error)}, status=400)
        return
    except Exception as error:  # noqa: BLE001 - surface engine failure as 500, don't leak a stack
        telemetry.increment("generate_nda_failed")
        handler._send_json({"error": f"NDA generation failed: {error}"}, status=500)
        return

    telemetry.increment("generate_nda_succeeded")
    handler._send_json(generated.response_payload(), status=201)
    stopwatch.mark("response sent", since_start=True)
