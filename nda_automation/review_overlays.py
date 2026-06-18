"""Shared additive review-overlay pipeline (the anti-ghost seam).

WHY THIS EXISTS
---------------
Several deterministic *coverage detectors* close gaps the AI review is contractually
told to ignore (e.g. ``law_forum_check`` flags a governing-law<->forum jurisdiction
split that the reviewer dismisses on purpose). Each such detector must add a review
signal the SAME safe way: it may ELEVATE a clean ``pass`` to ``review`` with a clear
reason, but it may NEVER downgrade, soften, force-FAIL, or override a stronger AI
verdict. This module factors that single contract out of ``law_forum_check`` so every
detector shares one audited elevation path instead of each re-implementing it (and
risking a "deterministic ghost").

THE ANTI-GHOST CONTRACT (enforced here, once, for every overlay)
----------------------------------------------------------------
``apply_review_overlays(review_state, matter)`` runs the law/forum overlay PLUS a
list of additive detectors over ``review_state``. For EACH overlay:

* Only a state that is currently a clean PASS ("pass") is ever upgraded -- to REVIEW.
* Any state already REVIEW or CHECK (or anything other than a clean pass) is returned
  UNCHANGED: an overlay never downgrades, never softens, never force-FAILs, and never
  overrides a stronger AI verdict. It is strictly a gap-filler that adds a review
  signal where the AI produced a clean pass.
* FAIL-SAFE: any detector exception is swallowed -- it can never crash the board poll
  -- and the (possibly already-elevated) state flows on to the next overlay untouched.

Pipeline ordering note: once any overlay has elevated the state to REVIEW, later
overlays see a non-pass state and (per the contract) leave it unchanged, but they
still get a chance to RUN -- their reason codes are additive, so we let each detector
that wants to contribute a code do so even after the state is already REVIEW. See
``apply_review_overlays`` for how that additive-code path works.

SHARED DETECTOR CONTRACT
------------------------
Each additive coverage detector is a callable ``detector(matter) -> dict | None``
that returns ``{"reason_code", "message"}`` (a finding) or ``None`` (silent). It must
be fail-safe on its own, but we ALSO wrap every call so a raising detector can never
break the pipeline.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from . import law_forum_check

# A coverage detector: takes a stored matter, returns a finding dict or None.
DetectorFn = Callable[[Mapping[str, Any]], "dict | None"]


def _elevate_pass_to_review(
    review_state: dict,
    *,
    reason_code: str,
    message: str,
) -> dict:
    """Return a NEW review_state elevated from PASS to REVIEW with ``reason_code``.

    Pure: builds a new dict, never mutates the input. The caller guarantees the
    input state is a clean PASS before calling this. The reason code is appended to
    ``reason_codes`` (deduped, additive) and a human-readable ``message`` recorded
    under ``overlay_review_reason`` / ``overlay_review_reasons`` so multiple overlays
    can each contribute a message without clobbering one another.
    """
    from .review_state import (  # noqa: PLC0415 -- avoid load-time cycle.
        REVIEW_STATE_REVIEW,
        _overall_status_for_state,
        _state_label,
        _state_tone,
    )

    elevated = dict(review_state)
    elevated["state"] = REVIEW_STATE_REVIEW
    elevated["overall_status"] = _overall_status_for_state(REVIEW_STATE_REVIEW)
    elevated["label"] = _state_label(REVIEW_STATE_REVIEW)
    elevated["tone"] = _state_tone(REVIEW_STATE_REVIEW)
    elevated["requires_attention"] = True
    elevated["requires_human_review"] = True
    elevated["blocks_send"] = True
    elevated["blocks_auto_send"] = True

    existing_codes = elevated.get("reason_codes")
    codes = list(existing_codes) if isinstance(existing_codes, list) else []
    if reason_code and reason_code not in codes:
        codes.append(reason_code)
    elevated["reason_codes"] = codes

    if message:
        existing_msgs = elevated.get("overlay_review_reasons")
        msgs = list(existing_msgs) if isinstance(existing_msgs, list) else []
        if message not in msgs:
            msgs.append(message)
        elevated["overlay_review_reasons"] = msgs
        # First overlay message is also surfaced as the primary scalar reason.
        elevated.setdefault("overlay_review_reason", message)
    return elevated


def _apply_additive_detector(
    review_state: dict | None,
    matter: Mapping[str, Any],
    detector: DetectorFn,
) -> dict | None:
    """Run one additive coverage detector and apply its finding (fail-safe).

    Contract per overlay:
      * If the current state is NOT a clean pass, the detector is still consulted so
        its reason_code can be appended (additive), but the state is NEVER changed --
        a stronger AI/earlier-overlay verdict is preserved exactly.
      * If the current state IS a clean pass and the detector returns a finding, the
        state is elevated pass -> review via ``_elevate_pass_to_review``.
      * Any exception (detector or elevation) returns the input state UNTOUCHED.
    """
    try:
        if not isinstance(review_state, dict):
            return review_state
        from .review_state import REVIEW_STATE_PASS  # noqa: PLC0415

        finding = detector(matter)
        if not isinstance(finding, dict):
            return review_state
        reason_code = str(finding.get("reason_code") or "").strip()
        message = str(finding.get("message") or finding.get("reason") or "").strip()

        current = str(review_state.get("state") or "").strip().lower()
        if current != REVIEW_STATE_PASS:
            # Already needs attention -> never change the state. Still record the
            # reason_code additively (a deduped append) so the finding is visible,
            # but leave every state/blocking field as the stronger verdict set it.
            if not reason_code:
                return review_state
            existing_codes = review_state.get("reason_codes")
            codes = list(existing_codes) if isinstance(existing_codes, list) else []
            if reason_code in codes:
                return review_state
            updated = dict(review_state)
            codes.append(reason_code)
            updated["reason_codes"] = codes
            return updated

        # Clean pass + a finding -> elevate to review.
        return _elevate_pass_to_review(
            review_state, reason_code=reason_code, message=message
        )
    except Exception:  # noqa: BLE001 -- fail-safe: never crash the poll; never alter on error.
        return review_state


def _coverage_detectors() -> list[DetectorFn]:
    """The list of additive coverage detectors, resolved lazily and fail-safe.

    Each detector module is imported optionally: a detector that has not been merged
    yet (import fails) or whose entry function is missing is simply skipped, so the
    pipeline degrades gracefully to whatever detectors are present. Wiring a new
    detector is a one-line addition to ``_SPECS`` below.
    """
    # (module attribute, function name) -- the SHARED DETECTOR CONTRACT.
    _specs: list[tuple[str, str]] = [
        ("notwithstanding_check", "detect_carveout_negation"),
        ("incorporation_check", "detect_incorporation_override"),
        ("definition_poison_check", "detect_definition_poison"),
    ]
    detectors: list[DetectorFn] = []
    for module_name, func_name in _specs:
        try:
            module = __import__(
                f"{__package__}.{module_name}", fromlist=[func_name]
            )
        except Exception:  # noqa: BLE001 -- detector not merged yet / import error.
            continue
        fn = getattr(module, func_name, None)
        if callable(fn):
            detectors.append(fn)
    return detectors


def apply_review_overlays(
    review_state: dict | None,
    matter: Mapping[str, Any],
) -> dict | None:
    """Run all additive review overlays over ``review_state`` (anti-ghost, fail-safe).

    Order:
      1. The law<->forum overlay (``law_forum_check.apply_lawforum_overlay``) -- kept
         first so its established behaviour and reason fields are unchanged.
      2. Each coverage detector in ``_coverage_detectors()``, applied additively.

    Every step obeys the SAME anti-ghost contract: only a clean PASS is ever elevated
    (to REVIEW), no step downgrades or overrides a stronger verdict, and any error is
    swallowed so the board poll can never crash. Returns the (possibly elevated)
    review_state; ``None``/non-dict inputs are returned unchanged.
    """
    if not isinstance(review_state, dict):
        return review_state

    # 1) Established law/forum overlay first (unchanged behaviour + reason fields).
    try:
        review_state = law_forum_check.apply_lawforum_overlay(review_state, matter)
    except Exception:  # noqa: BLE001 -- fail-safe.
        pass

    # 2) Additive coverage detectors.
    for detector in _coverage_detectors():
        review_state = _apply_additive_detector(review_state, matter, detector)

    return review_state
