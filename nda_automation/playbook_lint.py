"""Deterministic structural consistency lint for the playbook (Layer 1).

This module catches playbooks whose clause prose, structured ``rules``, and
redline templates are internally inconsistent -- the class of bug where a
``requirement`` states something the ``rules`` do not enforce, or a condition's
redline cannot be generated because the template it needs is absent.

It is purely deterministic: it inspects the playbook data structure only and
never calls the AI. The companion publish gate / CI test (owned by a separate
teammate) consumes :func:`lint_playbook` and the :data:`CHECK_IDS` registry.

Public API (stable -- the integration is built against this):

* ``lint_playbook(playbook) -> list[LintViolation]`` -- ``[]`` for a clean book.
* ``LintViolation`` -- dataclass(clause_id, check_id, message, severity="error").
* ``CHECK_IDS`` -- the tuple of registered check ids.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "LintViolation",
    "lint_playbook",
    "CHECK_IDS",
    "CHECKS",
    "VALID_ISSUE_TYPES",
    "VALID_REDLINE_ACTIONS",
    "REDLINE_ACTIONS_NEEDING_TEMPLATE",
]

# ---------------------------------------------------------------------------
# Vocabulary -- kept locally so the lint can run on a raw playbook mapping
# without importing the whole review engine. These mirror the canonical
# constants in ``ai_assessment_contract`` / ``redline_actions`` / ``checks.common``;
# a divergence test pins them so they cannot silently drift.
# ---------------------------------------------------------------------------

VALID_ISSUE_TYPES: frozenset[str] = frozenset(
    {"none", "missing", "present_but_wrong", "unclear"}
)
VALID_REDLINE_ACTIONS: frozenset[str] = frozenset(
    {"no_change", "replace_paragraph", "insert_after_paragraph", "delete_paragraph"}
)
VALID_DECISIONS: frozenset[str] = frozenset({"pass", "fail", "review"})

# Only these actions construct *new* paragraph text and therefore require a
# template (or an enumerated option source) to be generatable. ``delete_paragraph``
# and ``no_change`` need nothing.
REDLINE_ACTIONS_NEEDING_TEMPLATE: frozenset[str] = frozenset(
    {"replace_paragraph", "insert_after_paragraph"}
)

# Clause fields that can serve as the wording source for a generated redline.
TEMPLATE_FIELDS: tuple[str, ...] = ("redline_template", "standard_exclusions_template")

# The prose fields whose option/law/jurisdiction references imply an enumerated
# approved-option list must back them.
PROSE_FIELDS: tuple[str, ...] = (
    "requirement",
    "preferred_position",
    "check_trigger",
)


@dataclass(frozen=True)
class LintViolation:
    """A single structural inconsistency found in a clause.

    ``severity`` defaults to ``"error"``; a check may emit ``"warning"`` for a
    softer signal. The integration decides whether warnings block publish.
    """

    clause_id: str
    check_id: str
    message: str
    severity: str = "error"


# A check takes one clause mapping and yields zero or more violations.
CheckFn = Callable[[Mapping[str, Any]], list[LintViolation]]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    return str(value or "").strip()


def _clause_id(clause: Mapping[str, Any]) -> str:
    return _text(clause.get("id")) or "unknown"


def _rules(clause: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = clause.get("rules")
    return rules if isinstance(rules, Mapping) else {}


def _condition_list(rules: Mapping[str, Any], field: str) -> list[Mapping[str, Any]]:
    raw = rules.get(field)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _all_conditions(rules: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Return ``(field, condition)`` pairs across pass/fail/review lists."""

    pairs: list[tuple[str, Mapping[str, Any]]] = []
    for field in ("pass_conditions", "fail_conditions", "review_triggers"):
        for condition in _condition_list(rules, field):
            pairs.append((field, condition))
    return pairs


def _has_template(clause: Mapping[str, Any]) -> bool:
    return any(_text(clause.get(field)) for field in TEMPLATE_FIELDS)


def _dynamic_fallback_wording(clause: Mapping[str, Any]) -> str:
    """Wording a dynamic clause carries for building its redline."""

    fallback = clause.get("fallback")
    if isinstance(fallback, Mapping):
        return _text(fallback.get("wording"))
    return ""


def _option_source_names_options(clause: Mapping[str, Any]) -> bool:
    """True when redline_guidance points the redline at an enumerated option list."""

    rules = _rules(clause)
    guidance = rules.get("redline_guidance")
    if isinstance(guidance, Mapping):
        if _text(guidance.get("option_source")):
            return True
    return False


def _approved_options(clause: Mapping[str, Any]) -> list[Any]:
    """Non-empty approved-option list from rules.approved_options or the clause."""

    rules = _rules(clause)
    for source in (rules.get("approved_options"), clause.get("approved_options")):
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
            options = [item for item in source if item not in (None, "", {})]
            if options:
                return list(options)
    # governing_law carries its enumerated set as ``approved_laws``.
    laws = clause.get("approved_laws")
    if isinstance(laws, Sequence) and not isinstance(laws, (str, bytes)):
        cleaned = [law for law in laws if _text(law)]
        if cleaned:
            return list(cleaned)
    return []


def _approved_option_values(clause: Mapping[str, Any]) -> set[str]:
    """The comparable string values of a clause's approved options.

    An option may be a plain string (e.g. an ``approved_laws`` entry) or a dict
    of the shape ``{"id", "label", "value", ...}``; in the dict case the
    ``value`` (falling back to ``label``/``id``) is what callers select.
    """

    values: set[str] = set()
    for option in _approved_options(clause):
        if isinstance(option, Mapping):
            value = _text(option.get("value")) or _text(option.get("label")) or _text(
                option.get("id")
            )
            if value:
                values.add(value)
        else:
            text = _text(option)
            if text:
                values.add(text)
    return values


# A reference to an enumerated, pre-approved option set. We require the word
# "approved" (or "playbook") to sit next to option/law/jurisdiction vocabulary
# so generic mentions of "lawful" or "the law" do not trip the check.
_APPROVED_OPTION_PATTERN = re.compile(
    r"\b(?:approved|playbook|permitted|listed)\b[^.]{0,60}?"
    r"\b(?:option|options|jurisdiction|jurisdictions|law|laws|governing law)\b",
    re.IGNORECASE,
)


def _prose_references_approved_options(clause: Mapping[str, Any]) -> bool:
    parts: list[str] = [_text(clause.get(field)) for field in PROSE_FIELDS]
    rules = _rules(clause)
    parts.append(_text(rules.get("acceptable_position")))
    for _field, condition in _all_conditions(rules):
        parts.append(_text(condition.get("description")))
    blob = " ".join(part for part in parts if part)
    return bool(_APPROVED_OPTION_PATTERN.search(blob))


# ---------------------------------------------------------------------------
# Check 1: decision_space_coverage
# ---------------------------------------------------------------------------


def check_decision_space_coverage(clause: Mapping[str, Any]) -> list[LintViolation]:
    """Every clause's rules must allow BOTH a pass and a not-pass outcome.

    A clause that can only-ever-pass (no fail_conditions and no review_triggers)
    or can only-ever-be-flagged (no pass_conditions) is a dead rule set: the
    engine can never reach one of its decisions.
    """

    rules = _rules(clause)
    clause_id = _clause_id(clause)
    if not rules:
        return [
            LintViolation(
                clause_id,
                "decision_space_coverage",
                "clause has no rules block, so no decision outcome can be reached",
            )
        ]

    has_pass = bool(_condition_list(rules, "pass_conditions"))
    has_flag = bool(_condition_list(rules, "fail_conditions")) or bool(
        _condition_list(rules, "review_triggers")
    )

    violations: list[LintViolation] = []
    if not has_pass:
        violations.append(
            LintViolation(
                clause_id,
                "decision_space_coverage",
                "rules define no pass_conditions, so the clause can never pass "
                "(only-ever-flagged)",
            )
        )
    if not has_flag:
        violations.append(
            LintViolation(
                clause_id,
                "decision_space_coverage",
                "rules define no fail_conditions and no review_triggers, so the "
                "clause can only-ever-pass and never be flagged",
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Check 2: condition_well_formed
# ---------------------------------------------------------------------------

_EXPECTED_DECISION_BY_FIELD = {
    "pass_conditions": "pass",
    "fail_conditions": "fail",
    "review_triggers": "review",
}


def check_condition_well_formed(clause: Mapping[str, Any]) -> list[LintViolation]:
    """Every pass/fail/review condition is structurally well-formed.

    Required fields (id, decision, issue_type, description) present; ids unique
    within the clause; decision in the valid set and consistent with the list it
    lives in; issue_type in the valid set; redline_action, when present, in the
    valid set.
    """

    rules = _rules(clause)
    clause_id = _clause_id(clause)
    violations: list[LintViolation] = []
    seen_ids: set[str] = set()

    for field, condition in _all_conditions(rules):
        cond_id = _text(condition.get("id"))
        label = cond_id or f"<{field} entry>"

        if not cond_id:
            violations.append(
                LintViolation(
                    clause_id,
                    "condition_well_formed",
                    f"{field} condition is missing required field 'id'",
                )
            )
        else:
            normalized = cond_id.lower()
            if normalized in seen_ids:
                violations.append(
                    LintViolation(
                        clause_id,
                        "condition_well_formed",
                        f"duplicate condition id '{cond_id}' within the clause",
                    )
                )
            seen_ids.add(normalized)

        if not _text(condition.get("description")):
            violations.append(
                LintViolation(
                    clause_id,
                    "condition_well_formed",
                    f"condition '{label}' is missing required field 'description'",
                )
            )

        decision = _text(condition.get("decision"))
        if not decision:
            violations.append(
                LintViolation(
                    clause_id,
                    "condition_well_formed",
                    f"condition '{label}' is missing required field 'decision'",
                )
            )
        elif decision not in VALID_DECISIONS:
            violations.append(
                LintViolation(
                    clause_id,
                    "condition_well_formed",
                    f"condition '{label}' decision '{decision}' is not one of "
                    f"{sorted(VALID_DECISIONS)}",
                )
            )
        else:
            expected = _EXPECTED_DECISION_BY_FIELD[field]
            if decision != expected:
                violations.append(
                    LintViolation(
                        clause_id,
                        "condition_well_formed",
                        f"condition '{label}' in {field} has decision '{decision}' "
                        f"but must be '{expected}'",
                    )
                )

        issue_type = _text(condition.get("issue_type"))
        if not issue_type:
            violations.append(
                LintViolation(
                    clause_id,
                    "condition_well_formed",
                    f"condition '{label}' is missing required field 'issue_type'",
                )
            )
        elif issue_type not in VALID_ISSUE_TYPES:
            violations.append(
                LintViolation(
                    clause_id,
                    "condition_well_formed",
                    f"condition '{label}' issue_type '{issue_type}' is not one of "
                    f"{sorted(VALID_ISSUE_TYPES)}",
                )
            )

        if "redline_action" in condition:
            redline_action = _text(condition.get("redline_action"))
            if redline_action and redline_action not in VALID_REDLINE_ACTIONS:
                violations.append(
                    LintViolation(
                        clause_id,
                        "condition_well_formed",
                        f"condition '{label}' redline_action '{redline_action}' is "
                        f"not one of {sorted(VALID_REDLINE_ACTIONS)}",
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# Check 3: redline_template_present
# ---------------------------------------------------------------------------


def check_redline_template_present(clause: Mapping[str, Any]) -> list[LintViolation]:
    """A fix that builds new text must have a way to build it.

    Every fail/review condition whose redline_action is ``replace_paragraph`` or
    ``insert_after_paragraph`` requires the clause to carry the wording needed:
    a ``redline_template`` / ``standard_exclusions_template``, an enumerated
    ``approved_options`` set the redline draws from, or -- for dynamic clauses --
    fallback wording. ``delete_paragraph`` / ``no_change`` need no template.
    """

    rules = _rules(clause)
    clause_id = _clause_id(clause)
    violations: list[LintViolation] = []

    has_template = _has_template(clause)
    has_options = bool(_approved_options(clause)) and _option_source_names_options(clause)
    has_fallback_wording = bool(_dynamic_fallback_wording(clause))
    can_build = has_template or has_options or has_fallback_wording

    if can_build:
        return violations

    for field in ("fail_conditions", "review_triggers"):
        for condition in _condition_list(rules, field):
            redline_action = _text(condition.get("redline_action"))
            if redline_action in REDLINE_ACTIONS_NEEDING_TEMPLATE:
                cond_id = _text(condition.get("id")) or f"<{field} entry>"
                violations.append(
                    LintViolation(
                        clause_id,
                        "redline_template_present",
                        f"condition '{cond_id}' uses redline_action "
                        f"'{redline_action}' but the clause carries no template "
                        f"({', '.join(TEMPLATE_FIELDS)}), no approved-option source, "
                        "and no dynamic fallback wording -- the fix is ungeneratable",
                    )
                )
    return violations


# ---------------------------------------------------------------------------
# Check 4: approved_options_present
# ---------------------------------------------------------------------------


def check_approved_options_present(clause: Mapping[str, Any]) -> list[LintViolation]:
    """A clause that talks about approved options must enumerate them.

    If the prose (requirement/preferred_position/check_trigger/acceptable_position)
    or any condition references approved options/laws/jurisdictions, or the
    redline_guidance names an ``option_source``, then a non-empty enumerated
    option list (rules.approved_options, clause approved_options, or approved_laws)
    must exist. Otherwise the redline has nothing to choose from.
    """

    clause_id = _clause_id(clause)
    references_options = _prose_references_approved_options(clause)
    names_option_source = _option_source_names_options(clause)
    if not references_options and not names_option_source:
        return []

    if _approved_options(clause):
        return []

    if names_option_source:
        reason = "rules.redline_guidance names an option_source"
    else:
        reason = "clause prose references approved options/laws/jurisdictions"
    return [
        LintViolation(
            clause_id,
            "approved_options_present",
            f"{reason}, but no non-empty approved option list "
            "(rules.approved_options / approved_options / approved_laws) is defined",
        )
    ]


# ---------------------------------------------------------------------------
# Check 5: referential_integrity
# ---------------------------------------------------------------------------


def check_referential_integrity(clause: Mapping[str, Any]) -> list[LintViolation]:
    """Fields a condition or guidance relies on must resolve.

    * rules.redline_guidance.template_field must name a clause field that exists
      and is non-empty.
    * rules.redline_guidance.option_source must resolve to a non-empty option list.
    * governing_law's preferred_law (when present) must be one of approved_laws.
    """

    rules = _rules(clause)
    clause_id = _clause_id(clause)
    violations: list[LintViolation] = []

    guidance = rules.get("redline_guidance")
    if isinstance(guidance, Mapping):
        template_field = _text(guidance.get("template_field"))
        if template_field and not _text(clause.get(template_field)):
            violations.append(
                LintViolation(
                    clause_id,
                    "referential_integrity",
                    f"rules.redline_guidance.template_field names "
                    f"'{template_field}', but that clause field is absent or empty",
                )
            )
        option_source = _text(guidance.get("option_source"))
        if option_source and not _approved_options(clause):
            violations.append(
                LintViolation(
                    clause_id,
                    "referential_integrity",
                    f"rules.redline_guidance.option_source names "
                    f"'{option_source}', but no non-empty approved option list resolves",
                )
            )

    preferred_law = _text(clause.get("preferred_law"))
    if preferred_law:
        approved_values = _approved_option_values(clause)
        if approved_values and preferred_law not in approved_values:
            violations.append(
                LintViolation(
                    clause_id,
                    "referential_integrity",
                    f"preferred_law '{preferred_law}' is not one of the approved "
                    "options/laws",
                )
            )

    return violations


# ---------------------------------------------------------------------------
# Registry + entry point
# ---------------------------------------------------------------------------

CHECKS: dict[str, CheckFn] = {
    "decision_space_coverage": check_decision_space_coverage,
    "condition_well_formed": check_condition_well_formed,
    "redline_template_present": check_redline_template_present,
    "approved_options_present": check_approved_options_present,
    "referential_integrity": check_referential_integrity,
}

# Stable, ordered registry of the check ids the engine runs.
CHECK_IDS: tuple[str, ...] = tuple(CHECKS.keys())


def lint_playbook(playbook: Mapping[str, Any]) -> list[LintViolation]:
    """Run every deterministic structural check over the playbook's clauses.

    Returns an empty list for a clean playbook. Each clause is run through the
    full registry; violations are returned in clause order, then check order.
    """

    if not isinstance(playbook, Mapping):
        return [
            LintViolation(
                "<playbook>",
                "decision_space_coverage",
                "playbook must be a mapping",
            )
        ]

    clauses = playbook.get("clauses")
    if not isinstance(clauses, Sequence) or isinstance(clauses, (str, bytes)):
        return [
            LintViolation(
                "<playbook>",
                "decision_space_coverage",
                "playbook.clauses must be a list of clause objects",
            )
        ]

    violations: list[LintViolation] = []
    for clause in clauses:
        if not isinstance(clause, Mapping):
            continue
        for check in CHECKS.values():
            violations.extend(check(clause))
    return violations
