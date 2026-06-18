from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from nda_automation import ai_review, openrouter_usage, telemetry


def setup_function(_function):
    telemetry.reset()


def test_record_openrouter_usage_increments_counters_and_logs_json(capsys):
    payload = {
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 5,
            "total_tokens": 17,
            "cost": 0.00023,
        },
    }

    openrouter_usage.record_openrouter_usage(payload, feature="review", model="x-ai/grok-4.3")

    counters = telemetry.snapshot()["counters"]
    assert counters["openrouter_prompt_tokens"] == 12
    assert counters["openrouter_completion_tokens"] == 5
    assert counters["openrouter_total_tokens"] == 17
    assert counters["openrouter_prompt_tokens__feature__review"] == 12
    assert counters["openrouter_total_tokens__model__x-ai_grok-4.3"] == 17
    assert counters["openrouter_cost_micro_units"] == 230
    logged = json.loads(capsys.readouterr().out.strip())
    assert logged == {
        "event": "openrouter_usage",
        "feature": "review",
        "model": "x-ai/grok-4.3",
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "cost": 0.00023,
    }


def test_record_openrouter_usage_tolerates_missing_and_partial_usage(capsys):
    openrouter_usage.record_openrouter_usage(
        {"usage": {"prompt_tokens": "7", "completion_tokens": None}},
        feature="dashboard_assistant",
        model="x-ai/grok-4.3",
    )

    counters = telemetry.snapshot()["counters"]
    assert counters["openrouter_prompt_tokens"] == 7
    assert counters["openrouter_total_tokens"] == 7
    logged = json.loads(capsys.readouterr().out.strip())
    assert logged["prompt_tokens"] == 7
    assert logged["completion_tokens"] == 0
    assert logged["total_tokens"] == 7


def test_record_openrouter_usage_never_raises_when_telemetry_fails(capsys):
    with patch.object(openrouter_usage.telemetry, "increment", side_effect=RuntimeError("boom")):
        openrouter_usage.record_openrouter_usage(
            {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
            feature="review",
            model="x",
        )

    assert capsys.readouterr().out == ""


def test_ai_cost_summary_converts_micro_units_to_usd_and_groups_by_feature(capsys):
    # Record real spend for three features so the rollup must group + convert.
    openrouter_usage.record_openrouter_usage(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.12}},
        feature="review",
        model="x-ai/grok-4.3",
    )
    openrouter_usage.record_openrouter_usage(
        {"usage": {"prompt_tokens": 40, "completion_tokens": 10, "cost": 0.03}},
        feature="generation",
        model="deepseek/flash",
    )
    openrouter_usage.record_openrouter_usage(
        {"usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.0009}},
        feature="triage",
        model="x-ai/grok-4.3",
    )
    capsys.readouterr()  # drain the per-record JSON log lines

    summary = telemetry.ai_cost_summary(telemetry.snapshot()["counters"])

    # Grand total = 0.12 + 0.03 + 0.0009 = 0.1509 USD, micro = 150900.
    assert summary["currency"] == "USD"
    assert summary["total_cost_micro_units"] == 150900
    assert summary["total_usd"] == 0.1509
    assert summary["total_tokens"] == 150 + 50 + 10

    by_feature = {row["feature"]: row for row in summary["features"]}
    assert set(by_feature) == {"review", "generation", "triage"}
    # Per-feature USD is converted from the stored micro-USD counter.
    assert by_feature["review"]["cost_usd"] == 0.12
    assert by_feature["generation"]["cost_usd"] == 0.03
    assert by_feature["triage"]["cost_usd"] == 0.0009
    # Token secondary figure is carried per feature.
    assert by_feature["review"]["total_tokens"] == 150
    # Features are ordered by spend, biggest first.
    assert [row["feature"] for row in summary["features"]] == ["review", "generation", "triage"]
    # Honest cumulative-since-restart labelling, not a fabricated "today".
    assert "restart" in summary["note"].lower()
    assert "today" in summary["note"].lower()


def test_ai_cost_summary_empty_when_no_usage_recorded():
    summary = telemetry.ai_cost_summary(telemetry.snapshot()["counters"])
    assert summary["total_usd"] == 0
    assert summary["total_cost_micro_units"] == 0
    assert summary["total_tokens"] == 0
    assert summary["features"] == []


def test_ai_cost_summary_tolerates_token_only_feature_without_cost(capsys):
    # The provider may omit a cost field; tokens are still recorded. The feature must
    # still appear (with $0.00) rather than vanishing from the breakdown.
    openrouter_usage.record_openrouter_usage(
        {"usage": {"prompt_tokens": 9, "completion_tokens": 1}},
        feature="structure",
        model="deepseek/flash",
    )
    capsys.readouterr()

    summary = telemetry.ai_cost_summary(telemetry.snapshot()["counters"])
    by_feature = {row["feature"]: row for row in summary["features"]}
    assert by_feature["structure"]["cost_usd"] == 0
    assert by_feature["structure"]["total_tokens"] == 10
    assert summary["total_usd"] == 0


def test_openrouter_reviewer_records_usage_from_provider_response(capsys):
    verdict = {
        "decision": "pass",
        "confidence": 0.9,
        "reason": "Reciprocal obligations are present.",
        "cited_spans": [],
        "issues": [],
        "suggested_fix": "",
    }
    response = json.dumps({
        "choices": [{"message": {"content": json.dumps(verdict)}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 13, "total_tokens": 24},
    }).encode("utf-8")
    captured = []

    def urlopen(request, *args, **kwargs):
        captured.append(request)
        context_manager = MagicMock()
        context_manager.__enter__.return_value.read.return_value = response
        context_manager.__exit__.return_value = False
        return context_manager

    with patch("urllib.request.urlopen", urlopen):
        reviewer = ai_review.OpenRouterAIReviewer(api_key="k", model="x-ai/grok-4.3")
        assert reviewer({"task": "semantic_clause_crosscheck"}) == verdict

    assert len(captured) == 1
    counters = telemetry.snapshot()["counters"]
    assert counters["openrouter_total_tokens__feature__review"] == 24
    assert counters["openrouter_total_tokens__model__x-ai_grok-4.3"] == 24
    logged = json.loads(capsys.readouterr().out.strip())
    assert logged["event"] == "openrouter_usage"
    assert logged["feature"] == "review"
    assert logged["model"] == "x-ai/grok-4.3"
    assert logged["total_tokens"] == 24
