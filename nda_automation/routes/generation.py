"""Generate an outbound NDA from the draft-ui intake.

POST /api/generate-nda is what the Generator tab's "Generate NDA" button calls.
The endpoint is intentionally thin: HTTP details stay here, while payload
translation, generation, safety checking, persistence, and response shaping live
in ``nda_generation_workflow``.
"""

from __future__ import annotations

from .. import generation_priority, nda_generation, nda_generation_workflow, telemetry
from ..nda_generation import NdaGenerationError
from .common import request_owner_user_id


def handle_generate_nda(handler) -> None:
    telemetry.increment("generate_nda_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        # Mark this foreground generation as in flight for its whole duration so the
        # CPU-bound background producers (inbound AI-review worker pool, Gmail poller)
        # defer to it and never starve it of the single worker's GIL/CPU -- the
        # structural anti-starvation guard that keeps generate under the 45 s frontend
        # timeout regardless of background load.
        with generation_priority.generation_in_progress():
            generated = nda_generation_workflow.generate_nda_from_payload(
                payload,
                owner_user_id=request_owner_user_id(handler),
            )
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
