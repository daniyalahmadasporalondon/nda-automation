"""Tests for the deterministic playbook consistency lint (Layer 1).

Each check has a violating fixture (assert the violation is raised) and a clean
fixture (assert no violation for that check). A final test runs the lint over the
real, shipped playbook and *reports* its findings without hard-failing on them.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from nda_automation.checker import load_playbook
from nda_automation.playbook_lint import (
    CHECK_IDS,
    CHECKS,
    LintViolation,
    VALID_ISSUE_TYPES,
    VALID_REDLINE_ACTIONS,
    lint_playbook,
)


# ---------------------------------------------------------------------------
# Fixtures: a minimal but valid clause + playbook the lint considers clean.
# ---------------------------------------------------------------------------


def _clean_required_clause() -> dict[str, Any]:
    """A required clause whose prose, rules, and template are consistent."""

    return {
        "id": "sample_required",
        "name": "Sample Required Clause",
        "type": "required",
        "requirement": "The agreement must include a sample clause.",
        "preferred_position": "A clear sample clause is present.",
        "check_trigger": "The sample clause is missing or unclear.",
        "redline_template": "This Agreement includes the standard sample clause.",
        "rules": {
            "version": 1,
            "clause_type": "required",
            "acceptable_position": "A clear sample clause is present.",
            "pass_conditions": [
                {
                    "id": "sample_present",
                    "decision": "pass",
                    "issue_type": "none",
                    "description": "A clear sample clause is present.",
                    "redline_action": "no_change",
                }
            ],
            "fail_conditions": [
                {
                    "id": "sample_missing",
                    "decision": "fail",
                    "issue_type": "missing",
                    "description": "The sample clause is missing.",
                    "redline_action": "insert_after_paragraph",
                }
            ],
            "review_triggers": [
                {
                    "id": "sample_unclear",
                    "decision": "review",
                    "issue_type": "unclear",
                    "description": "The sample clause is unclear.",
                    "redline_action": "no_change",
                }
            ],
            "redline_guidance": {
                "default_action": "insert_after_paragraph",
                "template_field": "redline_template",
            },
        },
    }


def _clean_playbook() -> dict[str, Any]:
    return {
        "version": "1.0",
        "name": "Test Playbook",
        "clauses": [_clean_required_clause()],
    }


def _run_check(check_id: str, clause: dict[str, Any]) -> list[LintViolation]:
    return CHECKS[check_id](clause)


def _violations_for(check_id: str, violations: list[LintViolation]) -> list[LintViolation]:
    return [v for v in violations if v.check_id == check_id]


# ---------------------------------------------------------------------------
# Sanity: registry + clean baseline
# ---------------------------------------------------------------------------


def test_check_ids_match_registry() -> None:
    assert set(CHECK_IDS) == set(CHECKS.keys())
    assert tuple(CHECKS.keys()) == CHECK_IDS  # ordered + stable


def test_clean_clause_has_no_violations() -> None:
    assert _run_check("decision_space_coverage", _clean_required_clause()) == []
    assert _run_check("condition_well_formed", _clean_required_clause()) == []
    assert _run_check("redline_template_present", _clean_required_clause()) == []
    assert _run_check("approved_options_present", _clean_required_clause()) == []
    assert _run_check("referential_integrity", _clean_required_clause()) == []


def test_clean_playbook_lints_clean() -> None:
    assert lint_playbook(_clean_playbook()) == []


def test_lint_violation_default_severity_is_error() -> None:
    v = LintViolation("c", "decision_space_coverage", "msg")
    assert v.severity == "error"
    assert (v.clause_id, v.check_id, v.message) == ("c", "decision_space_coverage", "msg")


# ---------------------------------------------------------------------------
# Check 1: decision_space_coverage
# ---------------------------------------------------------------------------


def test_decision_space_coverage_clean() -> None:
    assert _run_check("decision_space_coverage", _clean_required_clause()) == []


def test_decision_space_coverage_only_ever_passes() -> None:
    clause = _clean_required_clause()
    clause["rules"]["fail_conditions"] = []
    clause["rules"]["review_triggers"] = []
    violations = _run_check("decision_space_coverage", clause)
    assert violations
    assert any("only-ever-pass" in v.message for v in violations)
    assert all(v.check_id == "decision_space_coverage" for v in violations)


def test_decision_space_coverage_only_ever_flagged() -> None:
    clause = _clean_required_clause()
    clause["rules"]["pass_conditions"] = []
    violations = _run_check("decision_space_coverage", clause)
    assert violations
    assert any("never pass" in v.message for v in violations)


def test_decision_space_coverage_missing_rules() -> None:
    clause = _clean_required_clause()
    del clause["rules"]
    violations = _run_check("decision_space_coverage", clause)
    assert len(violations) == 1
    assert "no rules block" in violations[0].message


# ---------------------------------------------------------------------------
# Check 2: condition_well_formed
# ---------------------------------------------------------------------------


def test_condition_well_formed_clean() -> None:
    assert _run_check("condition_well_formed", _clean_required_clause()) == []


def test_condition_well_formed_missing_required_field() -> None:
    clause = _clean_required_clause()
    del clause["rules"]["fail_conditions"][0]["description"]
    violations = _run_check("condition_well_formed", clause)
    assert any("missing required field 'description'" in v.message for v in violations)


def test_condition_well_formed_duplicate_ids() -> None:
    clause = _clean_required_clause()
    clause["rules"]["review_triggers"][0]["id"] = "sample_missing"  # collides w/ fail id
    violations = _run_check("condition_well_formed", clause)
    assert any("duplicate condition id 'sample_missing'" in v.message for v in violations)


def test_condition_well_formed_bad_decision() -> None:
    clause = _clean_required_clause()
    clause["rules"]["pass_conditions"][0]["decision"] = "maybe"
    violations = _run_check("condition_well_formed", clause)
    assert any("decision 'maybe' is not one of" in v.message for v in violations)


def test_condition_well_formed_decision_mismatches_list() -> None:
    clause = _clean_required_clause()
    # valid decision value, but wrong for the list it lives in
    clause["rules"]["fail_conditions"][0]["decision"] = "review"
    violations = _run_check("condition_well_formed", clause)
    assert any("must be 'fail'" in v.message for v in violations)


def test_condition_well_formed_bad_issue_type() -> None:
    clause = _clean_required_clause()
    clause["rules"]["fail_conditions"][0]["issue_type"] = "totally_invalid"
    violations = _run_check("condition_well_formed", clause)
    assert any("issue_type 'totally_invalid' is not one of" in v.message for v in violations)


def test_condition_well_formed_bad_redline_action() -> None:
    clause = _clean_required_clause()
    clause["rules"]["fail_conditions"][0]["redline_action"] = "explode_paragraph"
    violations = _run_check("condition_well_formed", clause)
    assert any("redline_action 'explode_paragraph' is not one of" in v.message for v in violations)


def test_condition_well_formed_absent_redline_action_is_ok() -> None:
    clause = _clean_required_clause()
    # review_trigger with no redline_action at all -> must not flag
    del clause["rules"]["review_triggers"][0]["redline_action"]
    assert _run_check("condition_well_formed", clause) == []


# ---------------------------------------------------------------------------
# Check 3: redline_template_present
# ---------------------------------------------------------------------------


def test_redline_template_present_clean() -> None:
    assert _run_check("redline_template_present", _clean_required_clause()) == []


def test_redline_template_present_missing_template_for_replace() -> None:
    clause = _clean_required_clause()
    del clause["redline_template"]  # no template at all
    # fail condition uses insert_after_paragraph -> needs a template
    violations = _run_check("redline_template_present", clause)
    assert violations
    assert any("ungeneratable" in v.message for v in violations)
    assert all(v.check_id == "redline_template_present" for v in violations)


def test_redline_template_present_delete_needs_no_template() -> None:
    clause = _clean_required_clause()
    del clause["redline_template"]
    # switch the only template-needing actions to delete/no_change
    clause["rules"]["fail_conditions"][0]["redline_action"] = "delete_paragraph"
    clause["rules"]["review_triggers"][0]["redline_action"] = "no_change"
    assert _run_check("redline_template_present", clause) == []


def test_redline_template_present_standard_exclusions_template_counts() -> None:
    clause = _clean_required_clause()
    del clause["redline_template"]
    clause["standard_exclusions_template"] = "Excluded info: ..."
    assert _run_check("redline_template_present", clause) == []


def test_redline_template_present_dynamic_fallback_wording_counts() -> None:
    clause = _clean_required_clause()
    del clause["redline_template"]
    clause["rules"]["fail_conditions"][0]["redline_action"] = "replace_paragraph"
    clause["fallback"] = {"redline_action": "replace_paragraph", "wording": "Replacement text."}
    assert _run_check("redline_template_present", clause) == []


def test_redline_template_present_option_source_counts() -> None:
    clause = _clean_required_clause()
    del clause["redline_template"]
    clause["rules"]["fail_conditions"][0]["redline_action"] = "replace_paragraph"
    clause["rules"]["redline_guidance"] = {
        "default_action": "replace_paragraph",
        "option_source": "approved_options",
    }
    clause["rules"]["approved_options"] = [
        {"id": "a", "label": "A", "value": "A", "default": True}
    ]
    assert _run_check("redline_template_present", clause) == []


# ---------------------------------------------------------------------------
# Check 4: approved_options_present
# ---------------------------------------------------------------------------


def _option_clause(*, with_options: bool) -> dict[str, Any]:
    clause = _clean_required_clause()
    clause["id"] = "sample_governing"
    clause["requirement"] = "Governing law must be one of the approved jurisdictions."
    clause["rules"]["redline_guidance"] = {
        "default_action": "replace_paragraph",
        "option_source": "approved_options",
    }
    clause["rules"]["fail_conditions"][0]["redline_action"] = "replace_paragraph"
    if with_options:
        clause["rules"]["approved_options"] = [
            {"id": "x", "label": "X", "value": "X", "default": True}
        ]
        # give it a template too so redline_template_present stays clean here
        clause["redline_template"] = "Governed by X law."
    return clause


def test_approved_options_present_clean_when_enumerated() -> None:
    assert _run_check("approved_options_present", _option_clause(with_options=True)) == []


def test_approved_options_present_prose_references_but_no_options() -> None:
    clause = _option_clause(with_options=False)
    # remove the option_source so we isolate the *prose* trigger
    clause["rules"]["redline_guidance"].pop("option_source")
    clause["rules"]["redline_guidance"]["template_field"] = "redline_template"
    clause["rules"]["fail_conditions"][0]["redline_action"] = "insert_after_paragraph"
    violations = _run_check("approved_options_present", clause)
    assert violations
    assert any("prose references approved" in v.message for v in violations)


def test_approved_options_present_option_source_but_no_options() -> None:
    clause = _option_clause(with_options=False)
    # neutralize the prose trigger so we isolate the option_source trigger
    clause["requirement"] = "A sample clause must be present."
    clause["preferred_position"] = "A clear sample clause is present."
    clause["check_trigger"] = "The sample clause is missing."
    violations = _run_check("approved_options_present", clause)
    assert violations
    assert any("names an option_source" in v.message for v in violations)


def test_approved_options_present_lawful_prose_is_not_a_trigger() -> None:
    """Generic words like 'lawful' must not trip the approved-option check."""

    clause = _clean_required_clause()
    clause["check_trigger"] = "The party fails to act in a lawful manner."
    assert _run_check("approved_options_present", clause) == []


def test_approved_options_present_approved_laws_satisfy() -> None:
    clause = _option_clause(with_options=False)
    clause["rules"].pop("approved_options", None)
    clause["approved_laws"] = ["India", "England and Wales"]
    assert _run_check("approved_options_present", clause) == []


# ---------------------------------------------------------------------------
# Check 5: referential_integrity
# ---------------------------------------------------------------------------


def test_referential_integrity_clean() -> None:
    assert _run_check("referential_integrity", _clean_required_clause()) == []


def test_referential_integrity_template_field_does_not_resolve() -> None:
    clause = _clean_required_clause()
    clause["rules"]["redline_guidance"]["template_field"] = "nonexistent_template"
    violations = _run_check("referential_integrity", clause)
    assert violations
    assert any("nonexistent_template" in v.message for v in violations)


def test_referential_integrity_option_source_does_not_resolve() -> None:
    clause = _clean_required_clause()
    clause["rules"]["redline_guidance"]["option_source"] = "approved_options"
    # no approved_options / approved_laws anywhere
    violations = _run_check("referential_integrity", clause)
    assert any("no non-empty approved option list resolves" in v.message for v in violations)


def test_referential_integrity_preferred_law_not_in_approved() -> None:
    clause = _clean_required_clause()
    clause["approved_laws"] = ["India", "Delaware"]
    clause["preferred_law"] = "California"
    violations = _run_check("referential_integrity", clause)
    assert any("preferred_law 'California'" in v.message for v in violations)


def test_referential_integrity_preferred_law_in_approved_options_dicts() -> None:
    """preferred_law must resolve against option *values*, not stringified dicts.

    This is the exact false positive the real-playbook run exposed.
    """

    clause = _clean_required_clause()
    clause["preferred_law"] = "England and Wales"
    clause["approved_laws"] = ["India", "Delaware", "England and Wales"]
    clause["rules"]["approved_options"] = [
        {"id": "india", "label": "India", "value": "India", "default": False},
        {"id": "delaware", "label": "Delaware", "value": "Delaware", "default": False},
        {
            "id": "england_and_wales",
            "label": "England and Wales",
            "value": "England and Wales",
            "default": True,
        },
    ]
    assert _run_check("referential_integrity", clause) == []


# ---------------------------------------------------------------------------
# Vocabulary drift pin: the local valid sets must match the canonical contract.
# ---------------------------------------------------------------------------


def test_local_vocabulary_matches_contract() -> None:
    from nda_automation.ai_assessment_contract import (
        AI_ASSESSMENT_ISSUE_TYPES,
        AI_ASSESSMENT_REDLINE_ACTIONS,
    )

    assert set(VALID_ISSUE_TYPES) == set(AI_ASSESSMENT_ISSUE_TYPES)
    assert set(VALID_REDLINE_ACTIONS) == set(AI_ASSESSMENT_REDLINE_ACTIONS)


# ---------------------------------------------------------------------------
# The real, shipped playbook: report findings, do NOT hard-fail on them.
# ---------------------------------------------------------------------------


def test_real_playbook_lint_reports_findings(capsys: pytest.CaptureFixture[str]) -> None:
    playbook = load_playbook()
    violations = lint_playbook(playbook)

    # Contract: always returns a list of LintViolation.
    assert isinstance(violations, list)
    assert all(isinstance(v, LintViolation) for v in violations)

    # Report (visible with -s); do NOT hard-fail on real-playbook findings here.
    print(f"\nReal-playbook lint findings: {len(violations)}")
    for v in violations:
        print(f"  [{v.severity}] {v.clause_id} :: {v.check_id} :: {v.message}")

    # Defensive: every reported check_id is a registered one.
    assert all(v.check_id in CHECK_IDS for v in violations)


def test_real_playbook_each_clause_runs_all_checks() -> None:
    """Smoke: the real playbook is a mapping with clauses and lints without error."""

    playbook = load_playbook()
    # Should not raise; re-running is stable.
    first = lint_playbook(copy.deepcopy(playbook))
    second = lint_playbook(copy.deepcopy(playbook))
    assert [(v.clause_id, v.check_id) for v in first] == [
        (v.clause_id, v.check_id) for v in second
    ]
