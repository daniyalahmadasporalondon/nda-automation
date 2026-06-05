"""Real-API eval harness — run fixtures through the active AI-first engine.

This is deliberately SEPARATE from CI. The deterministic gate
(``tests/test_review_eval.py``) is reproducible and provider-free; this harness
makes real network calls to the configured Gemini provider and reports how the
active AI-first review engine changes outcomes on
the same authored fixtures.

It compares, per clause fixture:
  * Deterministic baseline — rules engine directly, AI overlay off
  * Active engine          — current runtime engine, normally AI-first/fail-closed
  * Outcome changes         — active-engine verdict != deterministic verdict
  * false clears caught     — deterministic passed, expected fail/review, active engine caught it
  * review noise added      — deterministic passed, expected pass, active engine escalated anyway

Run (needs a configured provider key in settings or env; never prints the key):

    python -m tests.real_api_eval

Not collected by pytest (no ``test_`` prefix) and never run in CI.
"""
from __future__ import annotations

import os
from collections import Counter

from nda_automation.ai_assessor import assess_nda_with_ai, configured_ai_assessment_reviewer
from nda_automation.ai_review import AI_REVIEW_ENV_ENABLED, _ai_review_settings, _api_key_source
from nda_automation.checker import review_nda
from nda_automation.review_engine import (
    ACTIVE_REVIEW_ENGINE_ENV,
    REVIEW_ENGINE_AI_FIRST,
    review_nda_with_active_engine,
)
from tests.review_eval import classify, load_cases


def build_real_ai_first_review_func():
    """Return (ai_first_review_func | None, provider, key_source, model). Never returns the key."""
    settings = _ai_review_settings()  # same provider/model assembly production uses
    provider = str(settings.get("provider") or "").strip().lower()
    model = str(settings.get("model") or "")
    if not provider:
        return None, provider, "", model
    key_source = _api_key_source(provider)  # "environment" | "local_settings" | ""
    if not key_source:
        return None, provider, "", model
    try:
        reviewer = configured_ai_assessment_reviewer(settings)
    except Exception as exc:  # misconfigured settings -> treat as unavailable
        return None, provider, f"unavailable ({type(exc).__name__})", model

    def ai_first_review_func(text: str, *, paragraphs=None) -> dict:
        return assess_nda_with_ai(text, paragraphs=paragraphs, reviewer=reviewer)

    return ai_first_review_func, provider, key_source, model


def _focus_clause(result: dict, clause_id: str) -> dict | None:
    for clause in result.get("clauses", []):
        if str(clause.get("id")) == clause_id:
            return clause
    return None


def _decision_and_reason(clause: dict | None) -> tuple[str, str]:
    if clause is None:
        return "", ""
    return str(clause.get("decision") or ""), str(clause.get("reason_code") or "")


def deterministic_review(text: str) -> dict:
    previous_enabled = os.environ.get(AI_REVIEW_ENV_ENABLED)
    os.environ[AI_REVIEW_ENV_ENABLED] = ""
    try:
        return review_nda(text)
    finally:
        if previous_enabled is None:
            os.environ.pop(AI_REVIEW_ENV_ENABLED, None)
        else:
            os.environ[AI_REVIEW_ENV_ENABLED] = previous_enabled


def active_engine_review(text: str, ai_first_review_func) -> dict:
    previous_engine = os.environ.get(ACTIVE_REVIEW_ENGINE_ENV)
    os.environ[ACTIVE_REVIEW_ENGINE_ENV] = REVIEW_ENGINE_AI_FIRST
    try:
        return review_nda_with_active_engine(text, ai_first_review_func=ai_first_review_func)
    finally:
        if previous_engine is None:
            os.environ.pop(ACTIVE_REVIEW_ENGINE_ENV, None)
        else:
            os.environ[ACTIVE_REVIEW_ENGINE_ENV] = previous_engine


def run_case_real(case: dict, ai_first_review_func) -> dict:
    text = str(case["text"])
    clause_id = str(case["clause_id"])
    expected = case["expected"]

    baseline_clause = _focus_clause(deterministic_review(text), clause_id)
    baseline_decision, baseline_reason = _decision_and_reason(baseline_clause)

    row = {
        "name": str(case["name"]),
        "clause_id": clause_id,
        "high_risk": bool(case.get("high_risk")),
        "label_source": str(case.get("label_source") or "engineering"),
        "expected_decision": str(expected.get("decision") or ""),
        "baseline_decision": baseline_decision,
        "baseline_class": classify({
            "expected": expected,
            "actual_decision": baseline_decision,
            "actual_reason_code": baseline_reason,
        }),
        "active_ran": False,
        "active_error": "",
        "active_engine": "",
        "active_status": "",
        "active_decision": baseline_decision,
        "active_class": "",
    }

    if ai_first_review_func is None:
        return row

    try:
        active_result = active_engine_review(text, ai_first_review_func)
        active_clause = _focus_clause(active_result, clause_id)
    except Exception as exc:  # network/provider error: active-engine columns absent for this case
        row["active_error"] = type(exc).__name__
        return row

    active_decision, active_reason = _decision_and_reason(active_clause)
    metadata = active_result.get("active_review_engine")
    metadata = metadata if isinstance(metadata, dict) else {}
    row.update(
        active_ran=True,
        active_engine=str(metadata.get("executed_engine") or metadata.get("engine") or ""),
        active_status=str(metadata.get("status") or ""),
        active_decision=active_decision,
        active_class=classify({
            "expected": expected,
            "actual_decision": active_decision,
            "actual_reason_code": active_reason,
        }),
    )
    return row


def compare(rows: list[dict]) -> dict:
    active_rows = [r for r in rows if r["active_ran"]]
    changed = [r for r in active_rows if r["active_decision"] != r["baseline_decision"]]
    caught = [
        r
        for r in active_rows
        if r["baseline_decision"] == "pass"
        and r["expected_decision"] in {"fail", "review"}
        and r["active_decision"] in {"fail", "review"}
    ]
    noise = [
        r
        for r in active_rows
        if r["baseline_decision"] == "pass"
        and r["expected_decision"] == "pass"
        and r["active_decision"] in {"fail", "review"}
    ]
    return {
        "total": len(rows),
        "high_risk": sum(1 for r in rows if r["high_risk"]),
        "active_ran": len(active_rows),
        "active_errors": [r for r in rows if r["active_error"]],
        "active_status_breakdown": Counter(r["active_status"] for r in active_rows if r["active_status"]),
        "deterministic_baseline": Counter(r["baseline_class"] for r in rows),
        "baseline_on_active_rows": Counter(r["baseline_class"] for r in active_rows),
        "active_engine": Counter(r["active_class"] for r in active_rows),
        "changed": changed,
        "caught": caught,
        "noise": noise,
    }


def _acc(counter: Counter, total: int) -> str:
    if not total:
        return "n/a"
    return f"{100 * counter.get('correct', 0) / total:.0f}%"


def format_report(provider: str, model: str, key_source: str, rows: list[dict], stats: dict) -> str:
    lines = []
    lines.append(f"Real-API active-engine eval — provider={provider or '?'} model={model or '?'} key={key_source or 'NONE'}")
    lines.append(f"cases={stats['total']} (high_risk={stats['high_risk']}, active_ran={stats['active_ran']})")
    lines.append("")

    baseline = stats["deterministic_baseline"]
    header = f"{'':14}{'correct':>9}{'false_clear':>13}{'false_flag':>12}{'wrong_reason':>14}{'acc':>7}"
    lines.append(header)
    lines.append(
        f"{'Deterministic':14}{baseline.get('correct', 0):>9}{baseline.get('false_clear', 0):>13}"
        f"{baseline.get('false_flag', 0):>12}{baseline.get('wrong_reason_code', 0):>14}{_acc(baseline, stats['total']):>7}"
    )
    if stats["active_ran"]:
        active = stats["active_engine"]
        lines.append(
            f"{'Active engine':14}{active.get('correct', 0):>9}{active.get('false_clear', 0):>13}"
            f"{active.get('false_flag', 0):>12}{active.get('wrong_reason_code', 0):>14}{_acc(active, stats['active_ran']):>7}"
        )
        baseline_on_active = stats["baseline_on_active_rows"]
        baseline_acc = 100 * baseline_on_active.get("correct", 0) / stats["active_ran"]
        active_acc = 100 * active.get("correct", 0) / stats["active_ran"]
        lines.append(
            f"  Δ per-clause accuracy on the {stats['active_ran']} active-engine cases: "
            f"{baseline_acc:.0f}% -> {active_acc:.0f}% ({active_acc - baseline_acc:+.0f} pts)"
        )
    lines.append("")
    gaps = [r for r in rows if r["baseline_class"] == "false_clear"]
    lines.append(f"Deterministic false clears (the gaps active AI-first exists to catch): {len(gaps)}")
    for row in gaps:
        lines.append(f"    ! {row['clause_id']}: {row['name']} (expected {row['expected_decision']}, baseline=pass)")
    lines.append("")
    lines.append(
        f"Active-engine outcome changes: {len(stats['changed'])}/{stats['active_ran']}"
    )
    if stats["active_status_breakdown"]:
        breakdown = ", ".join(f"{status}×{count}" for status, count in stats["active_status_breakdown"].most_common())
        lines.append(f"  Active-engine status breakdown: {breakdown}")
    for row in stats["changed"]:
        lines.append(
            f"    ~ {row['clause_id']}: {row['name']} — baseline={row['baseline_decision']} "
            f"-> active={row['active_decision']} [{row['active_status'] or '?'}]"
        )

    lines.append(f"False clears CAUGHT by active engine (pass->review/fail, expected not-pass): {len(stats['caught'])}")
    for row in stats["caught"]:
        lines.append(f"    + {row['clause_id']}: {row['name']} (expected {row['expected_decision']})")

    lines.append(f"Review NOISE added by active engine (pass->review/fail, expected pass): {len(stats['noise'])}")
    for row in stats["noise"]:
        lines.append(f"    - {row['clause_id']}: {row['name']}")

    if stats["active_errors"]:
        kinds = Counter(r["active_error"] for r in stats["active_errors"])
        lines.append(f"Active-engine errors: {len(stats['active_errors'])} ({', '.join(f'{k}×{v}' for k, v in kinds.items())})")
    return "\n".join(lines)


def main() -> int:
    ai_first_review_func, provider, key_source, model = build_real_ai_first_review_func()
    cases = load_cases()
    limit = os.environ.get("NDA_EVAL_LIMIT", "").strip()
    if limit.isdigit():
        cases = cases[: int(limit)]
    if ai_first_review_func is None:
        print(
            f"No usable AI provider configured (provider={provider or '?'}, key={key_source or 'NONE'}).\n"
            "Configure a provider key in app settings or set the provider env var, then re-run:\n"
            "    python -m tests.real_api_eval\n"
            f"(Loaded {len(cases)} fixtures; deterministic baseline below.)\n"
        )

    rows = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case['name']} ({case['clause_id']})", flush=True)
        rows.append(run_case_real(case, ai_first_review_func))
    stats = compare(rows)
    print(format_report(provider, model, key_source, rows, stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
