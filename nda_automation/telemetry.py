from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()
_STARTED_AT = datetime.now(timezone.utc)
_COUNTERS: dict[str, int] = {}
# Gauges are LAST-VALUE / HIGH-WATER samples (e.g. per-review peak RSS), distinct
# from the monotonic counters above. Stored separately so the existing counters
# block and its health derivation are untouched.
_GAUGES: dict[str, float] = {}


def increment(counter: str, amount: int = 1) -> None:
    if amount <= 0:
        return
    with _LOCK:
        _COUNTERS[counter] = _COUNTERS.get(counter, 0) + amount


def set_gauge(name: str, value: float) -> None:
    """Record the LATEST value of a gauge (overwrites any prior sample).

    For point-in-time samples like the most recent per-review peak RSS. Silently
    ignores a non-finite / non-numeric value so a bad probe never corrupts the
    snapshot.
    """
    coerced = _coerce_gauge_value(value)
    if coerced is None:
        return
    with _LOCK:
        _GAUGES[name] = coerced


def gauge_max(name: str, value: float) -> None:
    """Record a HIGH-WATER gauge: keep the maximum value ever seen for ``name``.

    For peaks that should not regress within a process (e.g. the largest per-review
    peak RSS observed). Non-numeric / non-finite values are ignored.
    """
    coerced = _coerce_gauge_value(value)
    if coerced is None:
        return
    with _LOCK:
        existing = _GAUGES.get(name)
        if existing is None or coerced > existing:
            _GAUGES[name] = coerced


def _coerce_gauge_value(value: float) -> float | None:
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    # Reject NaN / +-inf so the snapshot stays JSON-clean and comparable.
    if coerced != coerced or coerced in (float("inf"), float("-inf")):
        return None
    return coerced


def snapshot() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with _LOCK:
        counters = dict(sorted(_COUNTERS.items()))
        gauges = dict(sorted(_GAUGES.items()))
    return {
        "started_at": _STARTED_AT.isoformat(),
        "checked_at": now.isoformat(),
        "uptime_seconds": max(0, int((now - _STARTED_AT).total_seconds())),
        "counters": counters,
        "gauges": gauges,
    }


def reset() -> None:
    global _STARTED_AT
    with _LOCK:
        _STARTED_AT = datetime.now(timezone.utc)
        _COUNTERS.clear()
        _GAUGES.clear()


# Counters watched as generic "operational failure" signals in the health
# summary. Each defaults to 0 when absent from the counters mapping.
_OTHER_FAILURE_COUNTERS = (
    "gmail_sync_failures",
    "gmail_sync_rate_limit_failures",
    "csrf_rejections",
    "host_header_rejections",
    "rate_limit_hits",
    "docx_export_content_failures",
    "docx_export_health_failures",
    "export_copy_failures",
    "ai_verifier_errors",
)

_HEALTH_NOTE = (
    "Counts are cumulative since process start. Telemetry is in-memory and "
    "resets on restart; these figures are NOT windowed."
)


def _rate(numerator: int, denominator: int) -> float:
    """Safe division that returns 0.0 when the denominator is zero."""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _count(counters: Mapping[str, int], key: str) -> int:
    try:
        return int(counters.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def health_summary(counters: Mapping[str, int]) -> dict[str, Any]:
    """Derive an AI-review / generation health summary from raw counters.

    Pure function: it reads only the supplied counters mapping (no globals),
    so it is trivially testable. All rates guard against division by zero and
    return 0.0 when their denominator is zero.

    Status thresholds (computed from ABSOLUTE failure counts first, which are
    more actionable than lifetime rates, then rates as a secondary signal):

      warn when ANY of:
        - review fail_closed             >= 3
        - review fail_closed_rate        >= 0.05 AND review attempted   >= 20
        - generation failed              >= 3
        - generation safety_gate_blocked >= 5
        - inbound_review queue_full      >= 1
        - inbound_review failed          >= 5
        - inbound_review failure_rate    >= 0.25 AND inbound attempted  >= 10
        - any 'other' failure counter    >= 10

      alert when ANY of:
        - review fail_closed             >= 10
        - review fail_closed_rate        >= 0.15 AND review attempted   >= 20
        - generation failure_rate        >= 0.25 AND generation requests >= 10

    `status` is the maximum severity triggered ("ok" < "warn" < "alert") and
    `alerts` explains each trigger in plain English. Counts are cumulative
    since process start (see `note`); true windowing is out of scope.
    """
    review_attempted = _count(counters, "active_review_ai_first_attempted")
    review_fail_closed = _count(counters, "active_review_ai_first_fail_closed")
    review_partial = _count(counters, "active_review_ai_first_partial")
    review = {
        "attempted": review_attempted,
        "completed": _count(counters, "active_review_ai_first_completed"),
        "failed": _count(counters, "active_review_ai_first_failed"),
        "fail_closed": review_fail_closed,
        "partial": review_partial,
        "deterministic_completed": _count(counters, "active_review_deterministic_completed"),
        "fail_closed_rate": _rate(review_fail_closed, review_attempted),
        "partial_rate": _rate(review_partial, review_attempted),
    }

    # Inbound auto-review pool health: a saturating queue (queue_full) or a high
    # failure rate is the OOM-era saturation signal we want surfaced to operators.
    inbound_completed = _count(counters, "inbound_ai_review_completed")
    inbound_failed = _count(counters, "inbound_ai_review_failed")
    inbound_queue_full = _count(counters, "inbound_ai_review_queue_full")
    inbound_schedule_failed = _count(counters, "inbound_ai_review_schedule_failed")
    inbound_attempted = inbound_completed + inbound_failed
    inbound_review = {
        "completed": inbound_completed,
        "failed": inbound_failed,
        "queue_full": inbound_queue_full,
        "schedule_failed": inbound_schedule_failed,
        "attempted": inbound_attempted,
        "failure_rate": _rate(inbound_failed, inbound_attempted),
    }

    generation_requests = _count(counters, "generate_nda_requests")
    generation_failed = _count(counters, "generate_nda_failed")
    generation_safety_gate_blocked = _count(counters, "generate_nda_safety_gate_blocked")
    generation = {
        "requests": generation_requests,
        "succeeded": _count(counters, "generate_nda_succeeded"),
        "rejected": _count(counters, "generate_nda_rejected"),
        "failed": generation_failed,
        "safety_gate_blocked": generation_safety_gate_blocked,
        "failure_rate": _rate(generation_failed, generation_requests),
        "gate_block_rate": _rate(generation_safety_gate_blocked, generation_requests),
    }

    other = {key: _count(counters, key) for key in _OTHER_FAILURE_COUNTERS}

    alerts: list[str] = []
    severity = 0  # 0=ok, 1=warn, 2=alert

    def _flag(level: int, message: str) -> None:
        nonlocal severity
        severity = max(severity, level)
        alerts.append(message)

    # ----- alert tier (most severe) -----
    if review_fail_closed >= 10:
        _flag(2, f"AI review has fail-closed {review_fail_closed} times since start.")
    if review["fail_closed_rate"] >= 0.15 and review_attempted >= 20:
        _flag(
            2,
            "AI review fail-closed rate is "
            f"{review['fail_closed_rate'] * 100:.0f}% over {review_attempted} attempts.",
        )
    if generation["failure_rate"] >= 0.25 and generation_requests >= 10:
        _flag(
            2,
            "NDA generation failure rate is "
            f"{generation['failure_rate'] * 100:.0f}% over {generation_requests} requests.",
        )

    # ----- warn tier -----
    if review_fail_closed >= 3:
        _flag(1, f"AI review has fail-closed {review_fail_closed} times since start.")
    if review["fail_closed_rate"] >= 0.05 and review_attempted >= 20:
        _flag(
            1,
            "AI review fail-closed rate is "
            f"{review['fail_closed_rate'] * 100:.0f}% over {review_attempted} attempts.",
        )
    if generation_failed >= 3:
        _flag(1, f"NDA generation has failed {generation_failed} times since start.")
    if generation_safety_gate_blocked >= 5:
        _flag(
            1,
            f"NDA generation safety gate has blocked {generation_safety_gate_blocked} drafts.",
        )
    # Inbound auto-review saturation / failure -- the queue filling up means imports
    # are arriving faster than the fixed pool drains, the OOM-era backpressure signal.
    if inbound_queue_full >= 1:
        _flag(
            1,
            f"Inbound auto-review queue hit its bound {inbound_queue_full} time(s); "
            "reviews are backing up faster than the worker pool drains.",
        )
    if inbound_failed >= 5:
        _flag(1, f"Inbound auto-review has failed {inbound_failed} times since start.")
    if inbound_review["failure_rate"] >= 0.25 and inbound_attempted >= 10:
        _flag(
            1,
            "Inbound auto-review failure rate is "
            f"{inbound_review['failure_rate'] * 100:.0f}% over {inbound_attempted} attempts.",
        )
    for key, value in other.items():
        if value >= 10:
            _flag(1, f"Operational failure counter '{key}' is {value}.")

    status = {0: "ok", 1: "warn", 2: "alert"}[severity]
    if not alerts:
        alerts = ["No AI-review or generation failure thresholds crossed."]

    return {
        "review": review,
        "inbound_review": inbound_review,
        "generation": generation,
        "other": other,
        "status": status,
        "alerts": alerts,
        "note": _HEALTH_NOTE,
    }
