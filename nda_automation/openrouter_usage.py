from __future__ import annotations

from collections.abc import Mapping
import json
import re
import sys
from typing import Any

from . import telemetry

_COUNTER_PREFIX = "openrouter"
_COST_MICRO_UNITS = 1_000_000


def record_openrouter_usage(payload: Mapping[str, Any], *, feature: str, model: str) -> None:
    """Best-effort OpenRouter token/cost accounting.

    Accounting must never affect the AI call that just completed, so every error
    is swallowed. The payload shape is provider-controlled and intentionally
    parsed defensively.
    """
    try:
        _record_openrouter_usage(payload, feature=feature, model=model)
    except Exception:  # noqa: BLE001 - telemetry must never break provider calls
        return


def _record_openrouter_usage(payload: Mapping[str, Any], *, feature: str, model: str) -> None:
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    usage_mapping = usage if isinstance(usage, Mapping) else {}
    prompt_tokens = _non_negative_int(usage_mapping.get("prompt_tokens"))
    completion_tokens = _non_negative_int(usage_mapping.get("completion_tokens"))
    total_tokens = _non_negative_int(usage_mapping.get("total_tokens"))
    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens

    normalized_feature = _telemetry_segment(feature)
    normalized_model = _telemetry_segment(model)
    for token_kind, amount in (
        ("prompt_tokens", prompt_tokens),
        ("completion_tokens", completion_tokens),
        ("total_tokens", total_tokens),
    ):
        _increment_usage_counter(token_kind, amount, feature=normalized_feature, model=normalized_model)

    event: dict[str, Any] = {
        "event": "openrouter_usage",
        "feature": str(feature or "unknown"),
        "model": str(model or "unknown"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    cost = _optional_cost(payload, usage_mapping)
    if cost is not None:
        event["cost"] = cost
        cost_micro_units = max(0, int(round(cost * _COST_MICRO_UNITS)))
        _increment_usage_counter("cost_micro_units", cost_micro_units, feature=normalized_feature, model=normalized_model)

    print(json.dumps(event, sort_keys=True, separators=(",", ":")), file=sys.stdout, flush=True)


def _increment_usage_counter(token_kind: str, amount: int, *, feature: str, model: str) -> None:
    if amount <= 0:
        return
    telemetry.increment(f"{_COUNTER_PREFIX}_{token_kind}", amount=amount)
    telemetry.increment(f"{_COUNTER_PREFIX}_{token_kind}__feature__{feature}", amount=amount)
    telemetry.increment(f"{_COUNTER_PREFIX}_{token_kind}__model__{model}", amount=amount)


def _non_negative_int(value: object) -> int:
    try:
        if value is None or value is False:
            return 0
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_cost(payload: Mapping[str, Any], usage: Mapping[str, Any]) -> float | None:
    for source in (usage, payload):
        for key in ("cost", "total_cost", "total_cost_usd"):
            value = source.get(key)
            if value is None:
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                return parsed
    return None


def _telemetry_segment(value: object) -> str:
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown").strip().lower()).strip("_.-")
    return segment or "unknown"
