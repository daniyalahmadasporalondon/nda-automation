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
