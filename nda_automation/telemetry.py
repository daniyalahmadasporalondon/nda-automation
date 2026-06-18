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


# OpenRouter usage counters are recorded by openrouter_usage.record_openrouter_usage
# as cumulative-since-start integers. Cost is stored in MICRO-USD (USD * 1e6) so it
# stays an integer counter; tokens are plain counts. Both are grouped by feature via
# the "<base>__feature__<name>" key suffix (and by model via "__model__<name>").
_OPENROUTER_COST_TOTAL = "openrouter_cost_micro_units"
_OPENROUTER_COST_FEATURE_PREFIX = "openrouter_cost_micro_units__feature__"
_OPENROUTER_TOKENS_TOTAL = "openrouter_total_tokens"
_OPENROUTER_TOKENS_FEATURE_PREFIX = "openrouter_total_tokens__feature__"
_COST_MICRO_PER_USD = 1_000_000

_COST_NOTE = (
    "Spend is cumulative since process start (since last restart). Telemetry is "
    "in-memory and resets on restart; these figures are NOT windowed, so this is a "
    'lifetime-since-restart total, not a per-day "today" number.'
)


def _usd_from_micro(micro_units: int) -> float:
    """Convert a micro-USD integer counter to dollars, rounded to the sub-cent.

    Cost is recorded as round(usd * 1e6) integers, so dividing back is exact at the
    micro level; we round to 4 dp (hundredth-of-a-cent) for a clean, artifact-free
    display value without losing the small per-call costs that matter in aggregate.
    """
    return round(micro_units / _COST_MICRO_PER_USD, 4)


def ai_cost_summary(counters: Mapping[str, int]) -> dict[str, Any]:
    """Roll up OpenRouter AI spend into a USD, per-feature breakdown.

    Pure function: reads only the supplied counters mapping (no globals), so it is
    trivially testable. Reads the cumulative ``openrouter_cost_micro_units`` counters
    that ``record_openrouter_usage`` writes, converts micro-USD to dollars, and groups
    by feature (reviewer, generation, triage, verifier, structure, semantic-lint,
    intake -- whatever features actually recorded usage).

    The returned ``total_usd`` is the authoritative grand total (the
    ``openrouter_cost_micro_units`` counter) so it never drifts from the per-feature
    rows even if a feature label changes. Each feature row also carries its token
    count as a secondary figure. Counts are cumulative since process start (see
    ``note``); a true per-day "today" number requires the separate windowing work.
    """
    total_micro = _count(counters, _OPENROUTER_COST_TOTAL)
    total_tokens = _count(counters, _OPENROUTER_TOKENS_TOTAL)

    cost_micro_by_feature: dict[str, int] = {}
    for key in counters:
        if key.startswith(_OPENROUTER_COST_FEATURE_PREFIX):
            feature = key[len(_OPENROUTER_COST_FEATURE_PREFIX):]
            if feature:
                cost_micro_by_feature[feature] = _count(counters, key)

    tokens_by_feature: dict[str, int] = {}
    for key in counters:
        if key.startswith(_OPENROUTER_TOKENS_FEATURE_PREFIX):
            feature = key[len(_OPENROUTER_TOKENS_FEATURE_PREFIX):]
            if feature:
                tokens_by_feature[feature] = _count(counters, key)

    # Order features by spend (descending), then name, so the biggest cost driver
    # leads the panel. Include zero-cost-but-token features so token-only usage still
    # shows (cost may be absent when the provider omits a cost field).
    feature_names = set(cost_micro_by_feature) | set(tokens_by_feature)
    features = [
        {
            "feature": name,
            "cost_usd": _usd_from_micro(cost_micro_by_feature.get(name, 0)),
            "cost_micro_units": cost_micro_by_feature.get(name, 0),
            "total_tokens": tokens_by_feature.get(name, 0),
        }
        for name in feature_names
    ]
    features.sort(key=lambda row: (-row["cost_micro_units"], row["feature"]))

    return {
        "total_usd": _usd_from_micro(total_micro),
        "total_cost_micro_units": total_micro,
        "total_tokens": total_tokens,
        "currency": "USD",
        "features": features,
        "note": _COST_NOTE,
    }


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
