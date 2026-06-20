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
        # A clause is only ever surfaced to the engine through its trigger terms,
        # so a clean clause must carry at least one (trigger_terms_present check).
        "search_terms": ["sample clause"],
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


@pytest.mark.parametrize(
    "wording",
    [
        "Disclosure is allowed as permitted or required by law.",
        "The receiving party may disclose if required by law.",
        "Information may be disclosed where permitted by law.",
        "Disclosure permitted to the extent required by applicable law or regulation.",
        "Disclosure as permitted or required by law, regulation, or court order.",
    ],
)
def test_approved_options_present_permitted_required_by_law_is_not_a_trigger(
    wording: str,
) -> None:
    """Standard 'permitted/required by law' carve-out prose must NOT be read as a
    reference to an enumerated approved-option set (regression for the publish
    hard-block false positive)."""

    clause = _clean_required_clause()
    # Place the carve-out both in clause prose and in a rules condition
    # description -- both are scanned by the approved-options check.
    clause["requirement"] = f"The agreement must include a sample clause. {wording}"
    clause["rules"]["fail_conditions"][0]["description"] = wording
    assert _run_check("approved_options_present", clause) == []


def test_permitted_listed_qualifying_options_still_triggers() -> None:
    """'permitted'/'listed' qualifying an enumerable list noun (option/
    jurisdiction) IS a genuine approved-option reference and must still fire when
    no option list is enumerated."""

    for wording in (
        "Governing law must be one of the permitted jurisdictions.",
        "Pick one of the listed options.",
    ):
        clause = _clean_required_clause()
        clause["requirement"] = wording
        violations = _run_check("approved_options_present", clause)
        assert violations, wording
        assert any("prose references approved" in v.message for v in violations)


def test_lawful_carveout_playbook_publishes_clean() -> None:
    """End-to-end repro: a clean playbook stays lint-clean (publishable) after a
    clause picks up standard 'as permitted or required by law' carve-out wording."""

    clause = _clean_required_clause()
    clause["requirement"] = (
        "The agreement must include a confidentiality clause. The receiving "
        "party may disclose Confidential Information as permitted or required "
        "by law, regulation, or court order."
    )
    clause["rules"]["review_triggers"][0]["description"] = (
        "Disclosure is made other than as permitted or required by law."
    )
    playbook = {
        "version": "1.0",
        "name": "Test Playbook",
        "clauses": [clause],
    }
    violations = lint_playbook(playbook)
    approved_option_violations = _violations_for("approved_options_present", violations)
    assert approved_option_violations == []
    assert violations == []


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
# Check 6: option_id_collision
# ---------------------------------------------------------------------------


def _governing_clause_with_options(options: list[Any]) -> dict[str, Any]:
    """A governing-law-shaped clause carrying the given approved-option dicts."""

    clause = _clean_required_clause()
    clause["id"] = "governing_law"
    clause["rules"]["approved_options"] = options
    return clause


def test_option_id_collision_clean_distinct_ids() -> None:
    clause = _governing_clause_with_options(
        [
            {"id": "india", "label": "India", "value": "India"},
            {"id": "delaware", "label": "Delaware", "value": "Delaware"},
            {"id": "england_and_wales", "label": "England and Wales", "value": "England and Wales"},
        ]
    )
    assert _run_check("option_id_collision", clause) == []


def test_option_id_collision_distinct_names_same_id_is_flagged() -> None:
    # "Ontario, Canada" and "Ontario Canada" both strip to "ontario_canada".
    clause = _governing_clause_with_options(
        [
            {"id": "ontario_canada", "label": "Ontario, Canada", "value": "Ontario, Canada"},
            {"id": "ontario_canada_2", "label": "Ontario Canada", "value": "Ontario Canada"},
        ]
    )
    violations = _run_check("option_id_collision", clause)
    assert len(violations) == 1
    v = violations[0]
    # Reported under the referential-integrity class so it flows through the gate.
    assert v.check_id == "referential_integrity"
    assert v.clause_id == "governing_law"
    # The colliding names are named, and the shared id is named.
    assert "Ontario, Canada" in v.message
    assert "Ontario Canada" in v.message
    assert "ontario_canada" in v.message


def test_option_id_collision_case_only_difference_is_flagged() -> None:
    clause = _governing_clause_with_options(
        [
            {"id": "difc", "label": "DIFC", "value": "DIFC"},
            {"id": "difc_lower", "label": "difc", "value": "difc"},
        ]
    )
    violations = _run_check("option_id_collision", clause)
    assert len(violations) == 1
    assert "'difc'" in violations[0].message
    assert "'DIFC'" in violations[0].message


def test_option_id_collision_string_approved_laws() -> None:
    # Plain-string options (approved_laws) collide too.
    clause = _clean_required_clause()
    clause["id"] = "governing_law"
    clause["approved_laws"] = ["England, Wales", "England Wales"]
    violations = _run_check("option_id_collision", clause)
    assert len(violations) == 1
    assert "england_wales" in violations[0].message


def test_option_id_collision_identical_label_value_is_not_a_collision() -> None:
    # A single option whose label == value must NOT count as two colliding names.
    clause = _governing_clause_with_options(
        [{"id": "india", "label": "India", "value": "India"}]
    )
    assert _run_check("option_id_collision", clause) == []


def test_option_id_collision_no_options_is_clean() -> None:
    # A clause without an approved-option set has nothing to collide.
    assert _run_check("option_id_collision", _clean_required_clause()) == []


def test_option_id_collision_lint_playbook_surfaces_it() -> None:
    # The collision flows through the top-level lint_playbook entry point.
    clause = _governing_clause_with_options(
        [
            {"id": "ontario_canada", "label": "Ontario, Canada", "value": "Ontario, Canada"},
            {"id": "ontario_canada_2", "label": "Ontario Canada", "value": "Ontario Canada"},
        ]
    )
    playbook = {"version": "1.0", "name": "Test", "clauses": [clause]}
    violations = lint_playbook(playbook)
    collision = [v for v in violations if "derive the same option id" in v.message]
    assert len(collision) == 1


def test_option_id_collision_mirrors_engine_option_id() -> None:
    """The lint's id derivation must not drift from the engine's ``_option_id``.

    This is the contract that makes the collision check meaningful: it flags the
    exact ids the engine (and the entity-registry join key) actually produce.
    """

    from nda_automation.playbook_lint import _derive_option_id
    from nda_automation.playbook_rules import _option_id

    samples = [
        "England and Wales",
        "Ontario, Canada",
        "DIFC",
        "difc",
        "England, Wales",
        "England Wales",
        "U.S.A.",
        "  spaced  ",
        "***",
        "123-Abc",
    ]
    for sample in samples:
        assert _derive_option_id(sample) == _option_id(sample), sample


def test_option_id_collision_duplicate_explicit_id_is_flagged() -> None:
    """GAP 1: two options sharing the EXPLICIT id (the generation join key) but
    mapping to DIFFERENT laws must be flagged -- one silently shadows the other.

    Skeptic-supplied repro: governing_law approved_options where two options both
    carry id 'india' but resolve to different values. Their derived NAME ids are
    distinct (india / delaware / england_and_wales), so the name-derivation check
    alone would NOT catch this; the explicit-id check must.
    """

    clause = _governing_clause_with_options(
        [
            {"id": "england_and_wales", "value": "England and Wales", "default": True},
            {"id": "india", "value": "India"},
            {"id": "india", "label": "Delaware", "value": "Delaware"},
        ]
    )
    violations = _run_check("option_id_collision", clause)
    assert len(violations) == 1
    v = violations[0]
    assert v.check_id == "referential_integrity"
    assert v.clause_id == "governing_law"
    # The shared explicit id and both competing values are named.
    assert "'india'" in v.message
    assert "India" in v.message
    assert "Delaware" in v.message
    assert "explicit id" in v.message


def test_option_id_collision_duplicate_explicit_id_engine_silently_drops() -> None:
    """The harm the GAP 1 check guards against: the generation join keyed on the
    explicit id silently drops one of two options sharing that id.

    This pins WHY the explicit-id collision is a real bug -- selecting 'india'
    would resolve to Delaware (the last writer wins), the wrong jurisdiction.
    """

    options = [
        {"id": "england_and_wales", "value": "England and Wales", "default": True},
        {"id": "india", "value": "India"},
        {"id": "india", "label": "Delaware", "value": "Delaware"},
    ]
    # Mirror nda_generation._approved_governing_law_options: resolved[id] = value.
    resolved: dict[str, str] = {}
    for option in options:
        option_id = str(option.get("id") or "").strip()
        value = str(option.get("value") or option.get("label") or "").strip()
        if option_id and value:
            resolved[option_id] = value
    # India -> Delaware silently shadowed: the 'india' join key now yields the
    # wrong jurisdiction, and only one of the two distinct laws survives.
    assert resolved["india"] == "Delaware"
    assert "India" not in resolved.values()
    assert len(resolved) == 2  # three options collapsed to two join keys


def test_option_id_collision_value_collision_distinct_labels_is_flagged() -> None:
    """GAP 2: two options whose VALUES collide under DISTINCT labels must be
    flagged -- the engine derives the id from VALUE first, so a label-only
    difference does not save them.

    Skeptic-supplied repro: both values derive _option_id == 'ontario_canada'.
    A label-first derivation (round-1) would see distinct labels and miss it.
    """

    clause = _governing_clause_with_options(
        [
            {"id": "opt_a", "label": "Ontario (Canada)", "value": "Ontario, Canada"},
            {"id": "opt_b", "label": "Province of Ontario", "value": "Ontario Canada"},
        ]
    )
    violations = _run_check("option_id_collision", clause)
    assert len(violations) == 1
    v = violations[0]
    assert v.check_id == "referential_integrity"
    assert "ontario_canada" in v.message
    # The colliding VALUES (not the labels) are what derive the shared id.
    assert "Ontario, Canada" in v.message
    assert "Ontario Canada" in v.message
    assert "derive the same option id" in v.message


def test_option_id_collision_distinct_values_same_label_path_is_clean() -> None:
    """Guard against value-first over-firing: options with distinct values but a
    shared label must NOT collide on the name path (value wins), and distinct
    explicit ids must NOT collide on the id path."""

    clause = _governing_clause_with_options(
        [
            {"id": "india", "label": "Region", "value": "India"},
            {"id": "delaware", "label": "Region", "value": "Delaware"},
        ]
    )
    assert _run_check("option_id_collision", clause) == []


def test_option_id_collision_identical_duplicate_rows_not_flagged() -> None:
    """Two byte-identical option rows (same explicit id AND same value) do not
    change which value the join resolves to, so they are not flagged as an
    explicit-id collision (only a genuine value disagreement is harmful)."""

    clause = _governing_clause_with_options(
        [
            {"id": "india", "value": "India"},
            {"id": "india", "value": "India"},
        ]
    )
    assert _run_check("option_id_collision", clause) == []


# ---------------------------------------------------------------------------
# Check 7: governing_law_forum_present
# ---------------------------------------------------------------------------


def test_governing_law_forum_present_clean() -> None:
    clause = _governing_clause_with_options(
        [
            {
                "id": "india",
                "label": "India",
                "value": "India",
                "forum_jurisdiction": "Mumbai, India",
            },
            {
                "id": "england_and_wales",
                "label": "England and Wales",
                "value": "England and Wales",
                "forum_jurisdiction": "England and Wales",
                "default": True,
            },
        ]
    )
    assert _run_check("governing_law_forum_present", clause) == []


def test_governing_law_forum_present_missing_forum_is_flagged() -> None:
    clause = _governing_clause_with_options(
        [
            {
                "id": "india",
                "label": "India",
                "value": "India",
                "forum_jurisdiction": "Mumbai, India",
            },
            # Newly-authored law with NO court/forum -- cannot be published.
            {"id": "singapore", "label": "Singapore", "value": "Singapore"},
        ]
    )
    violations = _run_check("governing_law_forum_present", clause)
    assert len(violations) == 1
    v = violations[0]
    assert v.check_id == "governing_law_forum_present"
    assert v.clause_id == "governing_law"
    assert "Singapore" in v.message
    assert "forum_jurisdiction" in v.message


def test_governing_law_forum_present_blank_forum_is_flagged() -> None:
    clause = _governing_clause_with_options(
        [
            {
                "id": "delaware",
                "label": "Delaware",
                "value": "Delaware",
                "forum_jurisdiction": "   ",
            },
        ]
    )
    violations = _run_check("governing_law_forum_present", clause)
    assert len(violations) == 1
    assert "Delaware" in violations[0].message


def test_governing_law_forum_present_only_applies_to_governing_law() -> None:
    # A non-governing-law clause carrying forumless options is a no-op.
    clause = _clean_required_clause()
    clause["rules"]["approved_options"] = [
        {"id": "opt", "label": "Opt", "value": "Opt"},
    ]
    assert _run_check("governing_law_forum_present", clause) == []


def test_governing_law_forum_present_flows_through_lint_playbook() -> None:
    clause = _governing_clause_with_options(
        [{"id": "singapore", "label": "Singapore", "value": "Singapore"}]
    )
    playbook = {"version": "1.0", "name": "Test", "clauses": [clause]}
    violations = lint_playbook(playbook)
    forum = [v for v in violations if v.check_id == "governing_law_forum_present"]
    assert len(forum) == 1


# ---------------------------------------------------------------------------
# Vocabulary drift pin: the local valid sets must match the canonical contract.
# ---------------------------------------------------------------------------


def test_local_vocabulary_matches_contract() -> None:
    from nda_automation.ai_assessment_contract import (
        AI_ASSESSMENT_ISSUE_TYPES,
        AI_ASSESSMENT_PARAGRAPH_REDLINE_ACTIONS,
        AI_ASSESSMENT_REDLINE_ACTIONS,
        AI_REDLINE_SPAN_ACTIONS,
    )

    assert set(VALID_ISSUE_TYPES) == set(AI_ASSESSMENT_ISSUE_TYPES)

    # The playbook's fallback redline vocabulary is the set of PARAGRAPH-level
    # actions only. The sentence-level ``strike_span``/``replace_span`` sugar is
    # AI-only: a fresh model response may emit it on the wire, but it is lowered
    # to ``replace_paragraph`` at parse time and must NEVER be a valid playbook
    # fallback action (the playbook has no span to anchor against). So the local
    # set must match the contract's PARAGRAPH actions, NOT the full wire set.
    assert set(VALID_REDLINE_ACTIONS) == set(AI_ASSESSMENT_PARAGRAPH_REDLINE_ACTIONS)

    # Pin the AI-only-ness of the span sugar from both directions so a genuine
    # drift (a span action leaking into the playbook vocabulary, or a paragraph
    # action being dropped from the contract) still fails this test.
    assert set(AI_REDLINE_SPAN_ACTIONS).isdisjoint(VALID_REDLINE_ACTIONS)
    assert set(VALID_REDLINE_ACTIONS) | set(AI_REDLINE_SPAN_ACTIONS) == set(
        AI_ASSESSMENT_REDLINE_ACTIONS
    )


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


def test_one_throwing_check_does_not_disable_the_others(monkeypatch) -> None:
    """#38: per-check isolation -- a single throwing check yields a BLOCKING
    violation and the OTHER checks still run, instead of the whole gate silently
    becoming a no-op."""
    import nda_automation.playbook_lint as lint_mod

    def boom(_clause):
        raise RuntimeError("unusual-but-legal clause")

    # Replace one registered check with a thrower; keep the rest intact.
    patched = dict(lint_mod.CHECKS)
    patched["decision_space_coverage"] = boom
    monkeypatch.setattr(lint_mod, "CHECKS", patched)

    # A clause that ALSO trips a different (still-live) check, so we can prove the
    # other checks were not disabled by the thrower.
    playbook = {
        "clauses": [
            {
                "id": "c1",
                "rules": {
                    "pass_conditions": [
                        {"id": "p", "decision": "WRONG", "issue_type": "none", "description": "x"}
                    ],
                    "fail_conditions": [
                        {"id": "f", "decision": "fail", "issue_type": "missing", "description": "y"}
                    ],
                },
            }
        ]
    }
    violations = lint_mod.lint_playbook(playbook)
    check_ids = {v.check_id for v in violations}
    # The thrower surfaced as a blocking violation (not silently dropped)...
    assert "decision_space_coverage" in check_ids
    assert any("raised" in v.message for v in violations)
    # ...AND the OTHER check (condition_well_formed) still ran on the same clause.
    assert "condition_well_formed" in check_ids


# ---------------------------------------------------------------------------
# Check 8: trigger_terms_present
# ---------------------------------------------------------------------------


def test_trigger_terms_present_clean_clause_passes() -> None:
    clause = _clean_required_clause()
    assert _run_check("trigger_terms_present", clause) == []


def test_trigger_terms_present_flags_missing_search_terms() -> None:
    clause = _clean_required_clause()
    clause.pop("search_terms", None)
    violations = _run_check("trigger_terms_present", clause)
    assert [v.check_id for v in violations] == ["trigger_terms_present"]
    assert violations[0].clause_id == "sample_required"


def test_trigger_terms_present_flags_empty_list() -> None:
    clause = _clean_required_clause()
    clause["search_terms"] = []
    assert len(_run_check("trigger_terms_present", clause)) == 1


def test_trigger_terms_present_flags_blank_only_terms() -> None:
    clause = _clean_required_clause()
    clause["search_terms"] = ["   ", ""]
    assert len(_run_check("trigger_terms_present", clause)) == 1


def test_trigger_terms_present_blocks_publish_via_lint_playbook() -> None:
    clause = _clean_required_clause()
    clause["search_terms"] = []
    playbook = {"version": "1.0", "name": "T", "clauses": [clause]}
    violations = lint_playbook(playbook)
    assert any(v.check_id == "trigger_terms_present" for v in violations)


def test_live_playbook_has_trigger_terms_for_every_clause() -> None:
    playbook = load_playbook()
    for clause in playbook["clauses"]:
        assert _run_check("trigger_terms_present", clause) == [], clause.get("id")
