from __future__ import annotations

import json

import pytest

from nda_automation import phase_observability
from nda_automation.ai_assessor import assess_nda_with_ai
from nda_automation.ai_assessor import InMemoryAssessmentReviewer
from nda_automation.phase_observability import (
    RENDER_PHASE_EVENT,
    REVIEW_PHASE_EVENT,
    PhaseTimer,
)
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH


SOURCE_TEXT = "\n\n".join(
    [
        "Each party may disclose Confidential Information to the other party under this Agreement.",
        '"Confidential Information" means non-public business, financial, technical, customer, '
        "supplier, pricing, market, product, proprietary and trade secret information disclosed by "
        "either party.",
        "This Agreement shall be governed by the laws of California.",
        "The confidentiality obligations survive for a fixed period of five years.",
        "Each party remains free to deal with third parties outside the Purpose.",
        "For Aspora Limited\nBy:\nName:\nTitle:\nDate:\nFor Counterparty Ltd\nBy:\nName:\nTitle:\nDate:",
    ]
)


def _assessment(clause_id, decision, *, paragraph_id="", quote="", proposed_redline=None):
    issue_type = "none" if decision == "pass" else "present_but_wrong"
    evidence = []
    if paragraph_id and quote:
        evidence.append(
            {"paragraph_id": paragraph_id, "quote": quote, "relevance": "Supports the verdict."}
        )
    return {
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": f"{clause_id} assessed using the playbook and cited evidence.",
        "evidence": evidence,
        "proposed_redline": proposed_redline or {"action": "no_change"},
        "confidence": 0.91,
        "blocks_send": decision == "review",
    }


def _complete_response():
    return {
        "assessments": [
            _assessment("mutuality", "pass", paragraph_id="p1", quote="Each party may disclose Confidential Information"),
            _assessment(
                "confidential_information",
                "pass",
                paragraph_id="p2",
                quote='"Confidential Information" means non-public business',
            ),
            _assessment(
                "governing_law",
                "fail",
                paragraph_id="p3",
                quote="laws of California",
                proposed_redline={
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p3",
                    "text": "This Agreement shall be governed by the laws of England and Wales.",
                    "jurisdiction": "England and Wales",
                },
            ),
            _assessment("term_and_survival", "pass", paragraph_id="p4", quote="fixed period of five years"),
            _assessment("non_circumvention", "pass"),
            _assessment("signatures", "pass", paragraph_id="p6", quote="For Aspora Limited"),
        ],
    }


def _phase_records(captured_out: str, event: str) -> list[dict]:
    records: list[dict] = []
    for line in captured_out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") == event:
            records.append(parsed)
    return records


# --- PhaseTimer unit behavior -------------------------------------------------


def test_phase_timer_emits_structured_record_with_event_phase_and_job_id(capsys):
    timer = PhaseTimer(REVIEW_PHASE_EVENT, request_id="job-abc123")
    with timer.phase("assessor"):
        pass
    timer.total()

    records = _phase_records(capsys.readouterr().out, REVIEW_PHASE_EVENT)
    assert [r["phase"] for r in records] == ["assessor", "total"]
    for record in records:
        assert record["event"] == REVIEW_PHASE_EVENT
        assert record["request_id"] == "job-abc123"
        assert isinstance(record["elapsed_ms"], (int, float))
        assert record["elapsed_ms"] >= 0
    # ``total`` is cumulative-since-start; a per-phase mark is not.
    assert records[0]["cumulative"] is False
    assert records[1]["cumulative"] is True


def test_phase_timer_auto_generates_request_id_when_omitted(capsys):
    timer = PhaseTimer(RENDER_PHASE_EVENT)
    assert isinstance(timer.request_id, str)
    assert timer.request_id
    timer.mark("convert")
    record = _phase_records(capsys.readouterr().out, RENDER_PHASE_EVENT)[0]
    assert record["request_id"] == timer.request_id


def test_phase_timer_is_fail_open_when_emit_breaks(monkeypatch, capsys):
    # A serialization/print failure inside the timer must never propagate into the
    # job it is observing.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("stdout exploded")

    monkeypatch.setattr(phase_observability.json, "dumps", _boom)
    timer = PhaseTimer(REVIEW_PHASE_EVENT, request_id="job-x")
    # Should not raise.
    with timer.phase("assessor"):
        pass
    timer.total()
    # No phase records survive the broken emit, but the body still ran.
    assert _phase_records(capsys.readouterr().out, REVIEW_PHASE_EVENT) == []


# --- Review path instrumentation ---------------------------------------------


def test_review_path_emits_phase_records_with_expected_names_and_shared_job_id(capsys):
    reviewer = InMemoryAssessmentReviewer(response=_complete_response())
    assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)

    records = _phase_records(capsys.readouterr().out, REVIEW_PHASE_EVENT)
    phases = [r["phase"] for r in records]
    # The assessor model call, the adversarial verifier pass, and the cumulative
    # total are each timed. (Structure validation is gated off by default.)
    assert "assessor" in phases
    assert "verifier" in phases
    assert "total" in phases
    # The assessor phase is emitted before the verifier, which is before the total.
    assert phases.index("assessor") < phases.index("verifier") < phases.index("total")
    # Every record for this review shares one correlating job id.
    job_ids = {r["request_id"] for r in records}
    assert len(job_ids) == 1
    (job_id,) = job_ids
    assert isinstance(job_id, str) and job_id
    # The total is the cumulative-since-start record.
    total_record = next(r for r in records if r["phase"] == "total")
    assert total_record["cumulative"] is True


def test_review_path_total_is_emitted_even_for_a_passing_review(capsys):
    response = {
        "assessments": [
            _assessment("mutuality", "pass", paragraph_id="p1", quote="Each party may disclose Confidential Information"),
            _assessment(
                "confidential_information",
                "pass",
                paragraph_id="p2",
                quote='"Confidential Information" means non-public business',
            ),
            _assessment(
                "governing_law",
                "pass",
                paragraph_id="p3",
                quote="laws of California",
            ),
            _assessment("term_and_survival", "pass", paragraph_id="p4", quote="fixed period of five years"),
            _assessment("non_circumvention", "pass"),
            _assessment("signatures", "pass", paragraph_id="p6", quote="For Aspora Limited"),
        ],
    }
    reviewer = InMemoryAssessmentReviewer(response=response)
    assess_nda_with_ai(SOURCE_TEXT, reviewer=reviewer)

    records = _phase_records(capsys.readouterr().out, REVIEW_PHASE_EVENT)
    assert any(r["phase"] == "total" for r in records)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
