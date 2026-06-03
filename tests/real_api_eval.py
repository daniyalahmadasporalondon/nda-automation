"""Real-API eval harness — run the fixture set through the *live* AI provider.

This is deliberately SEPARATE from CI. The deterministic gate
(``tests/test_review_eval.py``) scripts the AI for reproducibility; this harness
makes real network calls to the configured provider (Alibaba / OpenRouter /
Gemini) and reports how the blind second opinion changes outcomes on the same
authored fixtures.

It compares, per clause fixture:
  * Python only          — deterministic engine, AI off
  * Python + AI          — real blind second opinion, gated by the arbiter
  * AI disagreements      — AI verdict != Python's deterministic verdict
  * false clears caught   — Python passed, expected fail/review, AI escalated
  * review noise added     — Python passed, expected pass, AI escalated anyway

The arbiter's fail-floor means AI can only ever escalate pass -> review, so every
AI-driven outcome change is exactly one of {caught a false clear, added noise}.

Run (needs a configured provider key in settings or env; never prints the key):

    python -m tests.real_api_eval

Not collected by pytest (no ``test_`` prefix) and never run in CI.
"""
from __future__ import annotations

import os
from collections import Counter

from nda_automation import ai_review
from nda_automation.checker import review_nda
from tests.review_eval import classify, load_cases


def build_real_reviewer():
    """Return (reviewer | None, provider, key_source, model). Never returns the key."""
    settings = ai_review._ai_review_settings()  # same assembly production uses
    provider = str(settings.get("provider") or "").strip().lower()
    model = str(settings.get("model") or "")
    if not provider:
        return None, provider, "", model
    key_source = ai_review._api_key_source(provider)  # "environment" | "local_settings" | ""
    if not key_source:
        return None, provider, "", model
    try:
        reviewer = ai_review._configured_reviewer(settings)
    except Exception as exc:  # misconfigured settings -> treat as unavailable
        return None, provider, f"unavailable ({type(exc).__name__})", model
    return reviewer, provider, key_source, model


def _focus_clause(result: dict, clause_id: str) -> dict | None:
    for clause in result.get("clauses", []):
        if str(clause.get("id")) == clause_id:
            return clause
    return None


def _decision_and_reason(clause: dict | None) -> tuple[str, str]:
    if clause is None:
        return "", ""
    return str(clause.get("decision") or ""), str(clause.get("reason_code") or "")


def run_case_real(case: dict, reviewer) -> dict:
    text = str(case["text"])
    clause_id = str(case["clause_id"])
    expected = case["expected"]

    py_clause = _focus_clause(review_nda(text), clause_id)
    py_decision, py_reason = _decision_and_reason(py_clause)

    row = {
        "name": str(case["name"]),
        "clause_id": clause_id,
        "high_risk": bool(case.get("high_risk")),
        "label_source": str(case.get("label_source") or "engineering"),
        "expected_decision": str(expected.get("decision") or ""),
        "py_decision": py_decision,
        "py_class": classify({"expected": expected, "actual_decision": py_decision, "actual_reason_code": py_reason}),
        "ai_ran": False,
        "ai_error": "",
        "ai_status": "",
        "ai_decision": "",
        "final_decision": py_decision,
        "final_class": "",
    }

    if reviewer is None:
        return row

    try:
        ai_clause = _focus_clause(review_nda(text, ai_reviewer=reviewer), clause_id)
    except Exception as exc:  # network/provider error: arbiter would treat AI as absent
        row["ai_error"] = type(exc).__name__
        return row

    final_decision, final_reason = _decision_and_reason(ai_clause)
    analysis = ai_clause.get("ai_review_analysis") if isinstance(ai_clause, dict) else None
    analysis = analysis if isinstance(analysis, dict) else {}
    row.update(
        ai_ran=True,
        ai_status=str(analysis.get("status") or ""),
        ai_decision=str(analysis.get("ai_decision") or ""),
        final_decision=final_decision,
        final_class=classify(
            {"expected": expected, "actual_decision": final_decision, "actual_reason_code": final_reason}
        ),
    )
    return row


def compare(rows: list[dict]) -> dict:
    ai_rows = [r for r in rows if r["ai_ran"]]
    disagreements = [r for r in ai_rows if r["ai_decision"] and r["ai_decision"] != r["py_decision"]]
    escalated = [r for r in ai_rows if r["final_decision"] != r["py_decision"]]
    caught = [
        r
        for r in ai_rows
        if r["py_decision"] == "pass"
        and r["expected_decision"] in {"fail", "review"}
        and r["final_decision"] == "review"
    ]
    noise = [
        r
        for r in ai_rows
        if r["py_decision"] == "pass"
        and r["expected_decision"] == "pass"
        and r["final_decision"] == "review"
    ]
    return {
        "total": len(rows),
        "high_risk": sum(1 for r in rows if r["high_risk"]),
        "ai_ran": len(ai_rows),
        "ai_errors": [r for r in rows if r["ai_error"]],
        "invalid": [r for r in ai_rows if r["ai_status"] == "invalid"],
        "ai_status_breakdown": Counter(r["ai_status"] for r in ai_rows if r["ai_status"]),
        "python_only": Counter(r["py_class"] for r in rows),
        "python_only_on_ai_rows": Counter(r["py_class"] for r in ai_rows),
        "python_plus_ai": Counter(r["final_class"] for r in ai_rows),
        "disagreements": disagreements,
        "escalated": escalated,
        "caught": caught,
        "noise": noise,
    }


def _acc(counter: Counter, total: int) -> str:
    if not total:
        return "n/a"
    return f"{100 * counter.get('correct', 0) / total:.0f}%"


def format_report(provider: str, model: str, key_source: str, rows: list[dict], stats: dict) -> str:
    lines = []
    lines.append(f"Real-API eval — provider={provider or '?'} model={model or '?'} key={key_source or 'NONE'}")
    lines.append(f"cases={stats['total']} (high_risk={stats['high_risk']}, ai_ran={stats['ai_ran']})")
    lines.append("")

    py = stats["python_only"]
    header = f"{'':14}{'correct':>9}{'false_clear':>13}{'false_flag':>12}{'wrong_reason':>14}{'acc':>7}"
    lines.append(header)
    lines.append(
        f"{'Python only':14}{py.get('correct', 0):>9}{py.get('false_clear', 0):>13}"
        f"{py.get('false_flag', 0):>12}{py.get('wrong_reason_code', 0):>14}{_acc(py, stats['total']):>7}"
    )
    if stats["ai_ran"]:
        ai = stats["python_plus_ai"]
        lines.append(
            f"{'Python + AI':14}{ai.get('correct', 0):>9}{ai.get('false_clear', 0):>13}"
            f"{ai.get('false_flag', 0):>12}{ai.get('wrong_reason_code', 0):>14}{_acc(ai, stats['ai_ran']):>7}"
        )
        po = stats["python_only_on_ai_rows"]
        py_acc = 100 * po.get("correct", 0) / stats["ai_ran"]
        ai_acc = 100 * ai.get("correct", 0) / stats["ai_ran"]
        lines.append(
            f"  Δ per-clause accuracy on the {stats['ai_ran']} AI-run cases: "
            f"{py_acc:.0f}% -> {ai_acc:.0f}% ({ai_acc - py_acc:+.0f} pts)"
        )
    lines.append("")
    gaps = [r for r in rows if r["py_class"] == "false_clear"]
    lines.append(f"Python-only false clears (the gaps AI exists to catch): {len(gaps)}")
    for row in gaps:
        lines.append(f"    ! {row['clause_id']}: {row['name']} (expected {row['expected_decision']}, py=pass)")
    lines.append("")
    lines.append(
        f"AI disagreements: {len(stats['disagreements'])}/{stats['ai_ran']}  "
        f"(arbiter escalated: {len(stats['escalated'])}, "
        f"recorded-only/blocked by fail-floor: {len(stats['disagreements']) - len(stats['escalated'])})"
    )
    if stats["ai_status_breakdown"]:
        breakdown = ", ".join(f"{status}×{count}" for status, count in stats["ai_status_breakdown"].most_common())
        lines.append(f"  AI status breakdown: {breakdown}")
    lines.append(f"Invalid AI outputs (status=invalid): {len(stats['invalid'])}")

    lines.append(f"False clears CAUGHT by AI (pass->review, expected not-pass): {len(stats['caught'])}")
    for row in stats["caught"]:
        lines.append(f"    + {row['clause_id']}: {row['name']} (expected {row['expected_decision']})")

    lines.append(f"Review NOISE added by AI (pass->review, expected pass): {len(stats['noise'])}")
    for row in stats["noise"]:
        lines.append(f"    - {row['clause_id']}: {row['name']}")

    if stats["ai_errors"]:
        kinds = Counter(r["ai_error"] for r in stats["ai_errors"])
        lines.append(f"AI errors: {len(stats['ai_errors'])} ({', '.join(f'{k}×{v}' for k, v in kinds.items())})")
    return "\n".join(lines)


def main() -> int:
    # Ambient AI is forced off; we drive the AI explicitly via the injected reviewer,
    # so the Python-only column is a true deterministic baseline regardless of env.
    os.environ["NDA_AI_REVIEW_ENABLED"] = ""

    reviewer, provider, key_source, model = build_real_reviewer()
    cases = load_cases()
    limit = os.environ.get("NDA_EVAL_LIMIT", "").strip()
    if limit.isdigit():
        cases = cases[: int(limit)]
    if reviewer is None:
        print(
            f"No usable AI provider configured (provider={provider or '?'}, key={key_source or 'NONE'}).\n"
            "Configure a provider key in app settings or set the provider env var, then re-run:\n"
            "    python -m tests.real_api_eval\n"
            f"(Loaded {len(cases)} fixtures; Python-only baseline below.)\n"
        )

    rows = [run_case_real(case, reviewer) for case in cases]
    stats = compare(rows)
    print(format_report(provider, model, key_source, rows, stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
