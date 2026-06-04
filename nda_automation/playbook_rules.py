from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import Any

from .ai_assessment_contract import (
    AI_ASSESSMENT_ISSUE_TYPES,
    AI_ASSESSMENT_REDLINE_ACTIONS,
    AI_CLAUSE_ASSESSMENT_SCHEMA,
    AI_REDLINE_NO_CHANGE,
)
from .checks.common import (
    ISSUE_TYPE_MISSING,
    ISSUE_TYPE_NONE,
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ISSUE_TYPE_UNCLEAR,
)
from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .review_state import CLAUSE_DECISION_FAIL, CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW

PLAYBOOK_RULES_VERSION = 1

PLAYBOOK_RULE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "version": {"type": "integer", "const": PLAYBOOK_RULES_VERSION},
        "clause_type": {"type": "string", "enum": ["required", "prohibited"]},
        "acceptable_position": {"type": "string"},
        "pass_conditions": {"type": "array"},
        "fail_conditions": {"type": "array"},
        "review_triggers": {"type": "array"},
        "evidence_requirements": {"type": "object"},
        "redline_guidance": {"type": "object"},
        "approved_options": {"type": "array"},
    },
    "required": [
        "version",
        "clause_type",
        "acceptable_position",
        "pass_conditions",
        "fail_conditions",
        "review_triggers",
        "evidence_requirements",
        "redline_guidance",
    ],
    "additionalProperties": False,
}


class PlaybookRulesError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = [str(error) for error in errors if str(error).strip()]
        super().__init__("Playbook rules validation failed: " + "; ".join(self.errors))


def validate_playbook_rules(playbook: Mapping[str, Any]) -> None:
    errors: list[str] = []
    clauses = playbook.get("clauses")
    if not isinstance(clauses, list):
        raise PlaybookRulesError(["playbook clauses must be a list"])

    for clause in clauses:
        if not isinstance(clause, Mapping):
            errors.append("playbook clause must be an object")
            continue
        _validate_clause_rules(clause, errors)

    if errors:
        raise PlaybookRulesError(errors)


def playbook_rules_for_ai(playbook: Mapping[str, Any]) -> dict[str, Any]:
    validate_playbook_rules(playbook)
    clauses = playbook.get("clauses", [])
    return {
        "version": PLAYBOOK_RULES_VERSION,
        "assessment_schema": deepcopy(AI_CLAUSE_ASSESSMENT_SCHEMA),
        "clauses": [
            clause_rules_for_ai(clause)
            for clause in clauses
            if isinstance(clause, Mapping)
        ],
    }


def clause_rules_for_ai(clause: Mapping[str, Any]) -> dict[str, Any]:
    rules = clause.get("rules")
    return {
        "clause_id": str(clause.get("id") or ""),
        "name": str(clause.get("name") or ""),
        "type": str(clause.get("type") or ""),
        "requirement": str(clause.get("requirement") or ""),
        "preferred_position": str(clause.get("preferred_position") or ""),
        "check_trigger": str(clause.get("check_trigger") or ""),
        "rules": deepcopy(rules) if isinstance(rules, Mapping) else {},
    }


def _validate_clause_rules(clause: Mapping[str, Any], errors: list[str]) -> None:
    clause_id = str(clause.get("id") or "unknown").strip() or "unknown"
    rules = clause.get("rules")
    if not isinstance(rules, Mapping):
        errors.append(f"Playbook clause {clause_id} must include rules.")
        return

    allowed_keys = set(PLAYBOOK_RULE_SCHEMA["properties"])
    for key in rules:
        if str(key) not in allowed_keys:
            errors.append(f"Playbook clause {clause_id} rules has unsupported field {key}.")

    if _int_value(rules.get("version")) != PLAYBOOK_RULES_VERSION:
        errors.append(f"Playbook clause {clause_id} rules.version must be {PLAYBOOK_RULES_VERSION}.")
    clause_type = _text(rules.get("clause_type"))
    if clause_type not in {"required", "prohibited"}:
        errors.append(f"Playbook clause {clause_id} rules.clause_type must be required or prohibited.")
    if not _text(rules.get("acceptable_position")):
        errors.append(f"Playbook clause {clause_id} rules.acceptable_position must be text.")

    pass_conditions = _required_rule_list(rules, "pass_conditions", clause_id, errors)
    fail_conditions = _required_rule_list(rules, "fail_conditions", clause_id, errors)
    review_triggers = _required_rule_list(rules, "review_triggers", clause_id, errors)
    _validate_condition_list(
        pass_conditions,
        clause_id=clause_id,
        field="pass_conditions",
        expected_decision=CLAUSE_DECISION_PASS,
        allowed_issue_types={ISSUE_TYPE_NONE},
        require_redline_action=False,
        errors=errors,
    )
    _validate_condition_list(
        fail_conditions,
        clause_id=clause_id,
        field="fail_conditions",
        expected_decision=CLAUSE_DECISION_FAIL,
        allowed_issue_types={ISSUE_TYPE_MISSING, ISSUE_TYPE_PRESENT_BUT_WRONG, ISSUE_TYPE_UNCLEAR},
        require_redline_action=True,
        errors=errors,
    )
    _validate_condition_list(
        review_triggers,
        clause_id=clause_id,
        field="review_triggers",
        expected_decision=CLAUSE_DECISION_REVIEW,
        allowed_issue_types={ISSUE_TYPE_UNCLEAR},
        require_redline_action=False,
        errors=errors,
    )
    _validate_clause_type_rule_coverage(clause_id, clause_type, fail_conditions, errors)
    _validate_evidence_requirements(clause_id, rules.get("evidence_requirements"), errors)
    _validate_redline_guidance(clause_id, rules.get("redline_guidance"), errors)
    _validate_approved_options(clause, rules, errors)


def _required_rule_list(
    rules: Mapping[str, Any],
    field: str,
    clause_id: str,
    errors: list[str],
) -> list[Mapping[str, Any]]:
    raw_items = rules.get(field)
    if not isinstance(raw_items, list) or not raw_items:
        errors.append(f"Playbook clause {clause_id} rules.{field} must be a non-empty list.")
        return []
    return [item for item in raw_items if isinstance(item, Mapping)]


def _validate_condition_list(
    conditions: list[Mapping[str, Any]],
    *,
    clause_id: str,
    field: str,
    expected_decision: str,
    allowed_issue_types: set[str],
    require_redline_action: bool,
    errors: list[str],
) -> None:
    for index, condition in enumerate(conditions):
        prefix = f"Playbook clause {clause_id} rules.{field}[{index}]"
        condition_id = _text(condition.get("id"))
        if not condition_id:
            errors.append(f"{prefix} must include id.")
        if not _text(condition.get("description")):
            errors.append(f"{prefix} must include description.")
        decision = _text(condition.get("decision"))
        if decision != expected_decision:
            errors.append(f"{prefix} decision must be {expected_decision}.")
        issue_type = _text(condition.get("issue_type"))
        if issue_type not in allowed_issue_types:
            errors.append(f"{prefix} issue_type must be one of {', '.join(sorted(allowed_issue_types))}.")
        if issue_type and issue_type not in AI_ASSESSMENT_ISSUE_TYPES:
            errors.append(f"{prefix} issue_type is not supported by the AI assessment contract.")
        redline_action = _text(condition.get("redline_action"))
        if require_redline_action and not redline_action:
            errors.append(f"{prefix} must include redline_action.")
        if redline_action:
            if redline_action not in AI_ASSESSMENT_REDLINE_ACTIONS:
                errors.append(f"{prefix} redline_action is not supported by the AI assessment contract.")
            if expected_decision == CLAUSE_DECISION_PASS and redline_action != AI_REDLINE_NO_CHANGE:
                errors.append(f"{prefix} pass conditions must use redline_action no_change.")
            if expected_decision == CLAUSE_DECISION_FAIL and redline_action == AI_REDLINE_NO_CHANGE:
                errors.append(f"{prefix} fail conditions must use a redline action.")


def _validate_clause_type_rule_coverage(
    clause_id: str,
    clause_type: str,
    fail_conditions: list[Mapping[str, Any]],
    errors: list[str],
) -> None:
    fail_issue_types = {_text(condition.get("issue_type")) for condition in fail_conditions}
    if clause_type == "required":
        missing = {ISSUE_TYPE_MISSING, ISSUE_TYPE_PRESENT_BUT_WRONG} - fail_issue_types
        if missing:
            errors.append(
                f"Playbook clause {clause_id} required rules must cover fail issue_type(s): "
                + ", ".join(sorted(missing))
                + "."
            )
    elif clause_type == "prohibited":
        if ISSUE_TYPE_PRESENT_BUT_WRONG not in fail_issue_types:
            errors.append(f"Playbook clause {clause_id} prohibited rules must cover present_but_wrong.")


def _validate_evidence_requirements(clause_id: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"Playbook clause {clause_id} rules.evidence_requirements must be an object.")
        return
    if not isinstance(value.get("quote_required"), bool):
        errors.append(f"Playbook clause {clause_id} rules.evidence_requirements.quote_required must be boolean.")
    for key in ["minimum_evidence_for_pass", "minimum_evidence_for_fail"]:
        number = _int_value(value.get(key))
        if number is None or number < 0:
            errors.append(f"Playbook clause {clause_id} rules.evidence_requirements.{key} must be a non-negative integer.")
    if not _text(value.get("guidance")):
        errors.append(f"Playbook clause {clause_id} rules.evidence_requirements.guidance must be text.")


def _validate_redline_guidance(clause_id: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"Playbook clause {clause_id} rules.redline_guidance must be an object.")
        return
    default_action = _text(value.get("default_action"))
    if default_action not in AI_ASSESSMENT_REDLINE_ACTIONS:
        errors.append(f"Playbook clause {clause_id} rules.redline_guidance.default_action is unsupported.")
    if default_action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_INSERT_AFTER_PARAGRAPH} and not (
        _text(value.get("template_field")) or _text(value.get("option_source"))
    ):
        errors.append(
            f"Playbook clause {clause_id} rules.redline_guidance must name template_field or option_source for {default_action}."
        )


def _validate_approved_options(clause: Mapping[str, Any], rules: Mapping[str, Any], errors: list[str]) -> None:
    clause_id = _text(clause.get("id")) or "unknown"
    raw_options = rules.get("approved_options", [])
    if raw_options in (None, ""):
        raw_options = []
    if raw_options and not isinstance(raw_options, list):
        errors.append(f"Playbook clause {clause_id} rules.approved_options must be a list.")
        return
    options = [option for option in raw_options if isinstance(option, Mapping)]
    for index, option in enumerate(options):
        prefix = f"Playbook clause {clause_id} rules.approved_options[{index}]"
        for field in ["id", "label", "value"]:
            if not _text(option.get(field)):
                errors.append(f"{prefix} must include {field}.")
        if "default" in option and not isinstance(option.get("default"), bool):
            errors.append(f"{prefix}.default must be boolean.")

    if clause_id != "governing_law":
        return

    if not options:
        errors.append("Playbook clause governing_law rules.approved_options must include approved jurisdiction options.")
    defaults = [option for option in options if option.get("default") is True]
    if len(defaults) != 1:
        errors.append("Playbook clause governing_law rules.approved_options must have exactly one default option.")


def _text(value: object) -> str:
    return str(value or "").strip()


def _int_value(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
