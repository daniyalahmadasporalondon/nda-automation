"""Deterministic fixture eval for the NDA review pipeline.

This is a *regression* eval, not a measure of real-world legal accuracy. It runs
authored NDA/clause snippets through the real ``review_nda`` and scores the final
clause decision against an expected outcome. Cases may script the AI reviewer
(through ``review_nda``'s ``ai_reviewer`` parameter) so the blind-AI arbiter,
review-state, and reason codes are exercised deterministically -- no provider
call, no network, no quota.

What it measures (all fixture-relative, i.e. against the labels we authored):
- false clears   : expected fail/review but the pipeline returned pass (the
                   dangerous failure for a legal tool)
- false flags    : expected pass but the pipeline escalated
- missed escalations / review churn
- AI disagreement handling : a scripted AI dissent must escalate to review
- invalid-AI handling      : a bad-citation / low-confidence AI output must
                             escalate to review, never clear

Run as a report:  PYTHONPATH=. python -m tests.review_eval
Gate (CI):        pytest tests/test_review_eval.py
"""
from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nda_automation import ai_review
from nda_automation.ai_assessor import (
    _validate_ai_assessment_response,
    build_ai_assessment_packet,
    stub_ai_assessment_response,
)
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.checker import load_playbook, review_nda, validate_playbook

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / "tests" / "fixtures" / "review_eval_cases.json"

# Native clauses the deterministic engine still emits (and that a scripted AI
# reviewer can target on the review_nda path). non_circumvention is intentionally
# absent: it migrated to a dynamic (engine=="dynamic") clause that only the
# AI-first path produces, so its verifier cases run through _ai_first_verifier_result.
AI_TARGET_CLAUSE_IDS = {
    "mutuality",
    "confidential_information",
    "governing_law",
    "term_and_survival",
}

# Case kinds drive which metric buckets a case contributes to.
KIND_DETERMINISTIC = "deterministic"  # AI off; tests checkers + crosscheck
KIND_AI_AGREEMENT = "ai_agreement"  # scripted AI agrees -> must not escalate
KIND_AI_DISAGREEMENT = "ai_disagreement"  # scripted AI dissents -> must escalate
KIND_AI_INVALID = "ai_invalid"  # scripted AI cites badly / low conf -> must escalate
KIND_VERIFIER = "verifier"  # scripted adversarial verifier -> justify-or-refute a finding
KIND_AI_FIRST_REAL = "ai_first_real"  # REAL provider on the live AI-first path (key-gated)

# Real-AI cases hit a live provider, so they only run when an OpenRouter key is
# present; key-free CI skips them cleanly (they are filtered out before scoring).
AI_FIRST_REAL_ENV_KEY = "OPENROUTER_API_KEY"


def ai_first_real_enabled() -> bool:
    return bool(str(os.environ.get(AI_FIRST_REAL_ENV_KEY, "")).strip())


def _ai_disabled() -> ExitStack:
    """Force ambient AI off so an un-scripted baseline never calls a real provider.

    A scripted reviewer passed to ``review_nda`` still runs (apply_ai_review uses
    an explicit reviewer regardless of the enabled flag); this only suppresses the
    *configured* provider for un-scripted runs.
    """
    stack = ExitStack()
    stack.enter_context(patch.object(ai_review.app_settings, "ai_settings", return_value={"enabled": False}))
    stack.enter_context(patch.object(ai_review.app_settings, "stored_ai_api_key", return_value=""))
    stack.enter_context(
        patch.dict(
            os.environ,
            {
                "NDA_AI_REVIEW_ENABLED": "",
                "NDA_AI_PROVIDER": "",
                "OPENROUTER_API_KEY": "",
            },
            clear=False,
        )
    )
    return stack


def _valid_citation(packet: dict) -> list:
    paragraphs = packet.get("paragraphs") or []
    if not paragraphs:
        return []
    first = paragraphs[0]
    return [{
        "paragraph_id": first["id"],
        "quote": str(first["text"])[:80],
        "relevance": "Supports the scripted decision.",
    }]


def _scripted_reviewer(focus_clause_id: str, focus: dict, deterministic_by_clause: dict):
    """A deterministic stand-in for the AI.

    The focus clause gets the case's scripted response; every other clause echoes
    its deterministic decision with a valid citation so it confirms and adds no
    cross-clause noise. The mock never sees Python's verdict any differently than
    the real model would -- the packet itself is still blind; this just simulates
    "the AI happened to agree on the other clauses".
    """

    def reviewer(packet: dict) -> dict:
        clause_id = str(packet["clause"]["id"])
        if clause_id == focus_clause_id:
            decision = str(focus.get("decision") or "pass")
            confidence = float(focus.get("confidence", 0.95))
            if focus.get("invalid_citation"):
                cited = [{
                    "paragraph_id": (packet.get("paragraphs") or [{"id": "p0"}])[0]["id"],
                    "quote": "this quote does not appear anywhere in the supplied paragraph",
                    "relevance": "Deliberately invalid citation for the eval.",
                }]
            elif decision in {"pass", "fail"}:
                cited = _valid_citation(packet)
            else:
                cited = []
            return {
                "decision": decision,
                "confidence": confidence,
                "reason": str(focus.get("reason") or "Scripted AI decision for the eval."),
                "cited_spans": cited,
                "issues": list(focus.get("issues") or []),
                "suggested_fix": str(focus.get("suggested_fix") or ""),
            }
        # Non-focus clause: confirm the deterministic decision (blind echo).
        decision = str(deterministic_by_clause.get(clause_id) or "pass")
        return {
            "decision": decision,
            "confidence": 0.95,
            "reason": "Confirms the deterministic decision (non-focus clause).",
            "cited_spans": _valid_citation(packet) if decision in {"pass", "fail"} else [],
            "issues": [],
            "suggested_fix": "",
        }

    return reviewer


def _scripted_verifier(focus_clause_id: str, focus: dict):
    """A deterministic stand-in for the adversarial verifier.

    The focus clause gets the case's scripted verdict (affirm/refute/uncertain);
    every other clause is affirmed so it adds no noise. This crosses the real
    ai_verifier seam (review_nda's ``ai_verifier`` parameter) so the justify-or-
    refute overlay and its decision rewrites are exercised without a provider call.
    """

    def verifier(packet: dict) -> dict:
        clause_id = str((packet.get("clause_id") or ""))
        if clause_id == focus_clause_id:
            return {
                "verdict": str(focus.get("verdict") or "affirm"),
                "confidence": float(focus.get("confidence", 0.95)),
                "rationale": str(focus.get("rationale") or "Scripted verifier verdict for the eval."),
            }
        return {"verdict": "affirm", "confidence": 0.95, "rationale": "Confirms the engine finding (non-focus clause)."}

    return verifier


def _ai_first_verifier_result(
    text: str,
    verifier,
    *,
    focus_clause_id: str | None = None,
    reviewer_confidence: float | None = None,
) -> dict:
    """Run a verifier case through the AI-first path (key-free stub reviewer).

    non_circumvention is a dynamic (engine=="dynamic") clause now, so the
    deterministic ``review_nda`` no longer emits it -- only the AI-first pipeline
    does. We drive that pipeline with the deterministic, network-free stub reviewer
    (``stub_ai_assessment_response``), which fails a present prohibited restriction
    and passes otherwise, then layer the scripted ``ai_verifier`` overlay on top so
    the justify-or-refute decision rewrites are exercised exactly as in the shipping
    AI-first path. Mirrors ``assess_nda_with_ai`` but threads ``ai_verifier`` through.

    ``reviewer_confidence`` overrides the stub reviewer's confidence on the focus
    clause. Under PURE confidence-gating the verifier only second-looks a PASS the
    main AI is NOT confident about, so a verifier case that exercises "the verifier
    refutes a suspect PASS" must script a sub-threshold reviewer confidence (the stub
    otherwise hardcodes a confident 0.95 pass, which now correctly SKIPS the verifier).
    A FAIL/REVIEW focus needs no override -- those are always verified regardless.
    """
    playbook = load_playbook()
    validate_playbook(playbook)
    packet = build_ai_assessment_packet(text, playbook=playbook)
    raw = stub_ai_assessment_response(packet)
    assessments = _validate_ai_assessment_response(raw, playbook=playbook, packet=packet)
    if reviewer_confidence is not None and focus_clause_id is not None:
        for assessment in assessments:
            if str(assessment.get("clause_id") or "") == focus_clause_id:
                assessment["confidence"] = float(reviewer_confidence)
    return build_ai_first_review_result(
        text,
        assessments,
        playbook=playbook,
        ai_verifier=verifier,
    )


def _ai_first_real_result(text: str) -> dict:
    """Run a case through the LIVE AI-first path with the REAL configured provider.

    This is the SHIPPING path: a real model produces the clause assessments and
    ``build_ai_first_review_result`` finalizes them (no deterministic governing-law
    backstop -- that was removed once the primary AI proved it reliably fails an
    unapproved jurisdiction on its own). Used only for key-gated real-AI cases, so
    it exercises the actual model judgment the product relies on, not a stub.

    The reviewer is resolved explicitly from the OpenRouter key so the live path is
    reachable regardless of the ambient AI-review enabled flag (the eval suppresses
    the configured provider for un-scripted runs; this is the one place that opts in).
    """
    from nda_automation.ai_assessor import (
        AI_ASSESSMENT_STUB_ENV,
        assess_nda_with_ai,
        configured_ai_assessment_reviewer,
    )
    from nda_automation.ai_review import DEFAULT_OPENROUTER_MODEL

    # The CI conftest exports the key-free stub env globally; a real-AI case must
    # reach the actual provider, so suppress the stub for just this resolution.
    with patch.dict(os.environ, {AI_ASSESSMENT_STUB_ENV: ""}, clear=False):
        reviewer = configured_ai_assessment_reviewer(
            {"provider": "openrouter", "model": DEFAULT_OPENROUTER_MODEL, "timeout_seconds": 60}
        )
    return assess_nda_with_ai(text, reviewer=reviewer)


def run_case(case: dict) -> dict:
    text = str(case["text"])
    clause_id = str(case["clause_id"])
    kind = str(case.get("kind") or KIND_DETERMINISTIC)

    # The baseline is the pure-deterministic verdict (checkers + crosscheck), so the
    # non-focus echo map a scripted reviewer confirms is the AI-untouched, verifier-
    # untouched decision. The verifier overlay is exercised through the real review
    # path below, not baked into this map.
    baseline = review_nda(text, verify=False)
    deterministic_by_clause = {
        str(clause.get("id")): str(clause.get("decision")) for clause in baseline["clauses"]
    }

    if kind == KIND_DETERMINISTIC:
        result = review_nda(text)
    elif kind == KIND_VERIFIER:
        verifier_spec = dict(case.get("verifier") or {})
        verifier = _scripted_verifier(clause_id, verifier_spec)
        # PURE confidence-gating: a verifier case that refutes a suspect PASS must make
        # the reviewer NOT confident, else the confident-pass skip means the verifier
        # never runs. The fixture sets ``reviewer_confidence`` for those PASS cases.
        reviewer_confidence = verifier_spec.get("reviewer_confidence")
        result = _ai_first_verifier_result(
            text,
            verifier,
            focus_clause_id=clause_id,
            reviewer_confidence=(
                float(reviewer_confidence) if reviewer_confidence is not None else None
            ),
        )
    elif kind == KIND_AI_FIRST_REAL:
        result = _ai_first_real_result(text)
    else:
        focus = dict(case.get("ai") or {})
        reviewer = _scripted_reviewer(clause_id, focus, deterministic_by_clause)
        result = review_nda(text, ai_reviewer=reviewer)

    clause = next(item for item in result["clauses"] if str(item.get("id")) == clause_id)
    return {
        "name": str(case["name"]),
        "clause_id": clause_id,
        "kind": kind,
        "high_risk": bool(case.get("high_risk")),
        "gated": bool(case.get("gated", True)),
        "label_source": str(case.get("label_source") or "engineering"),
        "label_note": str(case.get("label_note") or ""),
        "expected": case["expected"],
        "actual_decision": str(clause.get("decision")),
        "actual_status": str(clause.get("status")),
        "actual_reason_code": str(clause.get("reason_code")),
        "deterministic_decision": deterministic_by_clause.get(clause_id),
    }


def classify(outcome: dict) -> str:
    expected = outcome["expected"]
    expected_decision = str(expected.get("decision"))
    actual = outcome["actual_decision"]
    if actual == expected_decision:
        # Decision matched; if a reason code was specified it must match too.
        expected_reason = expected.get("reason_code")
        if expected_reason and outcome["actual_reason_code"] != expected_reason:
            return "wrong_reason_code"
        return "correct"
    if expected_decision in {"fail", "review"} and actual == "pass":
        return "false_clear"
    if expected_decision == "pass" and actual in {"fail", "review"}:
        return "false_flag"
    return "wrong_state"


def run_eval(cases: list | None = None) -> dict:
    cases = cases if cases is not None else load_cases()
    # Real-AI cases hit a live provider. Skip them entirely when no key is present
    # so key-free CI stays green; when keyed, run them OUTSIDE _ai_disabled() (which
    # clears OPENROUTER_API_KEY) so the real reviewer is actually reachable.
    real_ai_enabled = ai_first_real_enabled()
    scripted_cases = [c for c in cases if str(c.get("kind") or "") != KIND_AI_FIRST_REAL]
    real_ai_cases = [
        c for c in cases
        if str(c.get("kind") or "") == KIND_AI_FIRST_REAL and real_ai_enabled
    ]
    raw_outcomes: list = []
    with _ai_disabled():
        raw_outcomes.extend(run_case(case) for case in scripted_cases)
    raw_outcomes.extend(run_case(case) for case in real_ai_cases)
    outcomes = [dict(outcome, classification=classify(outcome)) for outcome in raw_outcomes]
    return summarize(outcomes)


def load_cases() -> list:
    with CASES_PATH.open(encoding="utf-8") as handle:
        cases = json.load(handle)
    if not isinstance(cases, list):
        raise ValueError("review_eval_cases.json must be a JSON list")
    return cases


def summarize(outcomes: list) -> dict:
    gated = [o for o in outcomes if o.get("gated", True)]
    observations = [o for o in outcomes if not o.get("gated", True)]

    per_clause: dict = {}
    for outcome in gated:
        bucket = per_clause.setdefault(
            outcome["clause_id"],
            {"total": 0, "correct": 0, "false_clears": [], "false_flags": [], "wrong": []},
        )
        bucket["total"] += 1
        result = outcome["classification"]
        if result == "correct":
            bucket["correct"] += 1
        elif result == "false_clear":
            bucket["false_clears"].append(outcome["name"])
        elif result == "false_flag":
            bucket["false_flags"].append(outcome["name"])
        else:
            bucket["wrong"].append(f"{outcome['name']} ({result})")

    disagreement = [o for o in gated if o["kind"] == KIND_AI_DISAGREEMENT]
    invalid = [o for o in gated if o["kind"] == KIND_AI_INVALID]
    return {
        "outcomes": outcomes,
        "gated": gated,
        "observations": observations,
        "per_clause": per_clause,
        "totals": {
            "cases": len(gated),
            "correct": sum(1 for o in gated if o["classification"] == "correct"),
            "false_clears": sum(1 for o in gated if o["classification"] == "false_clear"),
            "false_flags": sum(1 for o in gated if o["classification"] == "false_flag"),
            "wrong_state": sum(1 for o in gated if o["classification"] in {"wrong_state", "wrong_reason_code"}),
        },
        "ai_handling": {
            "disagreement_escalated": sum(1 for o in disagreement if o["actual_decision"] == "review"),
            "disagreement_total": len(disagreement),
            "invalid_escalated": sum(1 for o in invalid if o["actual_decision"] == "review"),
            "invalid_total": len(invalid),
        },
    }


def gate_failures(summary: dict) -> list:
    """Build-breaking violations among gated cases: any false clear, any
    high-risk miss, and any AI dissent / invalid-AI output that failed to
    escalate. Ungated (needs-counsel) observations never break the build."""
    failures: list = []
    for outcome in summary["gated"]:
        result = outcome["classification"]
        if result == "false_clear":
            failures.append(f"FALSE CLEAR: {outcome['name']} ({outcome['clause_id']}) expected "
                            f"{outcome['expected'].get('decision')} got pass")
        if outcome["high_risk"] and result != "correct":
            failures.append(f"HIGH-RISK REGRESSION: {outcome['name']} ({outcome['clause_id']}) "
                            f"expected {outcome['expected'].get('decision')} got {outcome['actual_decision']} [{result}]")
        if outcome["kind"] == KIND_AI_DISAGREEMENT and outcome["actual_decision"] != "review":
            failures.append(f"AI DISSENT NOT ESCALATED: {outcome['name']} got {outcome['actual_decision']}")
        if outcome["kind"] == KIND_AI_INVALID and outcome["actual_decision"] != "review":
            failures.append(f"INVALID AI NOT ESCALATED: {outcome['name']} got {outcome['actual_decision']}")
    # De-dup while preserving order.
    seen = set()
    deduped = []
    for failure in failures:
        if failure in seen:
            continue
        seen.add(failure)
        deduped.append(failure)
    return deduped


def format_report(summary: dict) -> str:
    lines = ["NDA review fixture eval (regression-relative, not real-world accuracy)", "=" * 72]
    totals = summary["totals"]
    lines.append(
        f"cases={totals['cases']}  correct={totals['correct']}  "
        f"false_clears={totals['false_clears']}  false_flags={totals['false_flags']}  "
        f"wrong_state={totals['wrong_state']}"
    )
    ai = summary["ai_handling"]
    lines.append(
        f"AI dissent escalated {ai['disagreement_escalated']}/{ai['disagreement_total']}  "
        f"invalid-AI escalated {ai['invalid_escalated']}/{ai['invalid_total']}"
    )
    by_label: dict = {}
    for outcome in summary["gated"]:
        by_label[outcome["label_source"]] = by_label.get(outcome["label_source"], 0) + 1
    if by_label:
        lines.append("gated by label authority: " + ", ".join(f"{src}={count}" for src, count in sorted(by_label.items())))
    lines.append("-" * 72)
    for clause_id in sorted(summary["per_clause"]):
        bucket = summary["per_clause"][clause_id]
        lines.append(f"{clause_id:26} {bucket['correct']}/{bucket['total']} correct")
        if bucket["false_clears"]:
            lines.append(f"    FALSE CLEARS: {', '.join(bucket['false_clears'])}")
        if bucket["false_flags"]:
            lines.append(f"    false flags:  {', '.join(bucket['false_flags'])}")
        if bucket["wrong"]:
            lines.append(f"    wrong:        {', '.join(bucket['wrong'])}")
    observations = summary.get("observations") or []
    if observations:
        lines.append("-" * 72)
        lines.append("ungated observations (need a counsel label; do NOT gate the build):")
        for outcome in observations:
            note = f" -- {outcome['label_note']}" if outcome.get("label_note") else ""
            lines.append(
                f"    {outcome['name']} ({outcome['clause_id']}): "
                f"engine={outcome['actual_decision']}/{outcome['actual_reason_code']}  "
                f"provisional-expected={outcome['expected'].get('decision')}  "
                f"[{outcome['classification']}]{note}"
            )
    failures = gate_failures(summary)
    lines.append("-" * 72)
    lines.append(f"GATE: {'PASS' if not failures else 'FAIL'}")
    for failure in failures:
        lines.append(f"  ! {failure}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report(run_eval()))
