from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()
_STARTED_AT = datetime.now(timezone.utc)
_COUNTERS: dict[str, int] = {}


def increment(counter: str, amount: int = 1) -> None:
    if amount <= 0:
        return
    with _LOCK:
        _COUNTERS[counter] = _COUNTERS.get(counter, 0) + amount


def snapshot() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with _LOCK:
        counters = dict(sorted(_COUNTERS.items()))
    return {
        "started_at": _STARTED_AT.isoformat(),
        "checked_at": now.isoformat(),
        "uptime_seconds": max(0, int((now - _STARTED_AT).total_seconds())),
        "counters": counters,
    }


def reset() -> None:
    global _STARTED_AT
    with _LOCK:
        _STARTED_AT = datetime.now(timezone.utc)
        _COUNTERS.clear()


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
    for key, value in other.items():
        if value >= 10:
            _flag(1, f"Operational failure counter '{key}' is {value}.")

    status = {0: "ok", 1: "warn", 2: "alert"}[severity]
    if not alerts:
        alerts = ["No AI-review or generation failure thresholds crossed."]

    return {
        "review": review,
        "generation": generation,
        "other": other,
        "status": status,
        "alerts": alerts,
        "note": _HEALTH_NOTE,
    }
