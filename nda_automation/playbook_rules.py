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
PLAYBOOK_POLICY_SCHEMA_VERSION = 1

CORE_REQUIRED_TEXT_FIELDS = ["id", "name", "requirement", "type", "preferred_position", "check_trigger"]

CORE_CLAUSE_FIELDS = {
    "id",
    "name",
    "requirement",
    "type",
    "preferred_position",
    "check_trigger",
    "acceptable_language",
    "rationale",
    "evidence_guidance",
    "search_terms",
    "taxonomy_groups",
    "semantic_signals",
    "rules",
}

CLAUSE_POLICY_FIELDS: dict[str, set[str]] = {
    "mutuality": {
        "one_way_terms",
        "redline_template",
        "role_reciprocity_terms",
        "role_terms",
    },
    "confidential_information": {
        "allowed_exclusions",
        "definition_categories",
        "exclusion_context_terms",
        "independent_development_qualification_terms",
        "independent_development_terms",
        "problematic_exclusion_terms",
        "redline_template",
        "standard_exclusions_template",
    },
    "governing_law": {
        "approved_laws",
        "law_phrases",
        "preferred_law",
    },
    "term_and_survival": {
        "indefinite_terms",
        "longer_survival_carve_out_terms",
        "max_term_years",
        "redline_template",
    },
    "non_circumvention": set(),
    "signatures": {
        "redline_template",
    },
}

CLAUSE_TEXT_LIST_FIELDS = {
    "allowed_exclusions",
    "approved_laws",
    "definition_categories",
    "exclusion_context_terms",
    "indefinite_terms",
    "independent_development_qualification_terms",
    "independent_development_terms",
    "longer_survival_carve_out_terms",
    "one_way_terms",
    "problematic_exclusion_terms",
    "role_reciprocity_terms",
    "role_terms",
    "search_terms",
    "semantic_signals",
    "taxonomy_groups",
}

PLAYBOOK_POLICY_SCHEMA: dict[str, object] = {
    "version": PLAYBOOK_POLICY_SCHEMA_VERSION,
    "top_level": {
        "required_text": ["name", "version"],
        "required_array": ["clauses"],
    },
    "clause": {
        "allowed_fields": sorted(CORE_CLAUSE_FIELDS),
        "required_text": CORE_REQUIRED_TEXT_FIELDS,
        "required_text_lists": ["search_terms"],
        "optional_text_lists": ["taxonomy_groups", "semantic_signals"],
        "types": ["required", "prohibited"],
    },
    "clause_overrides": {
        clause_id: sorted(fields)
        for clause_id, fields in CLAUSE_POLICY_FIELDS.items()
    },
    "governing_law": {
        "required_text_lists": ["approved_laws"],
        "required_mapping": ["law_phrases"],
        "preferred_field": "preferred_law",
        "rules_option_source": "approved_laws",
    },
    "term_and_survival": {
        "max_term_years": {"type": "integer", "minimum": 1, "maximum": 25},
        "required_text_lists": ["indefinite_terms", "longer_survival_carve_out_terms"],
    },
}

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
    _validate_playbook_policy_schema(playbook, errors)
    clauses = playbook.get("clauses")
    if not isinstance(clauses, list):
        if errors:
            raise PlaybookRulesError(errors)
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


def _validate_playbook_policy_schema(playbook: Mapping[str, Any], errors: list[str]) -> None:
    if not _text(playbook.get("name")):
        errors.append("Playbook name must be text.")
    if not _text(playbook.get("version")):
        errors.append("Playbook version must be text.")
    clauses = playbook.get("clauses")
    if not isinstance(clauses, list):
        errors.append("Playbook clauses must be a list.")
        return
    if not clauses:
        errors.append("Playbook clauses must not be empty.")
        return

    seen_clause_ids: set[str] = set()
    for index, clause in enumerate(clauses):
        if not isinstance(clause, Mapping):
            errors.append(f"Playbook clauses[{index}] must be an object.")
            continue
        _validate_clause_policy_schema(clause, errors)
        clause_id = _text(clause.get("id"))
        if not clause_id:
            continue
        normalized = clause_id.lower()
        if normalized in seen_clause_ids:
            errors.append(f"Playbook clause {clause_id} id must be unique.")
        seen_clause_ids.add(normalized)


def _validate_clause_policy_schema(clause: Mapping[str, Any], errors: list[str]) -> None:
    clause_id = _text(clause.get("id")) or "unknown"
    allowed_fields = CORE_CLAUSE_FIELDS | CLAUSE_POLICY_FIELDS.get(clause_id, set())
    unknown_fields = sorted(str(field) for field in clause.keys() if str(field) not in allowed_fields)
    if clause_id in CLAUSE_POLICY_FIELDS and unknown_fields:
        errors.append(f"Playbook clause {clause_id} has unsupported field(s): {', '.join(unknown_fields)}.")

    for field in CORE_REQUIRED_TEXT_FIELDS:
        if not _text(clause.get(field)):
            errors.append(f"Playbook clause {clause_id} must include {field}.")
    if _text(clause.get("type")) not in {"required", "prohibited"}:
        errors.append(f"Playbook clause {clause_id} type must be required or prohibited.")
    _validate_text_list_field(clause, "search_terms", clause_id, errors, required=True)
    for field in sorted(CLAUSE_TEXT_LIST_FIELDS - {"search_terms"}):
        if field in clause:
            _validate_text_list_field(clause, field, clause_id, errors, required=False)

    if clause_id == "governing_law":
        _validate_governing_law_policy_schema(clause, errors)
    elif clause_id == "term_and_survival":
        _validate_term_survival_policy_schema(clause, errors)


def _validate_governing_law_policy_schema(clause: Mapping[str, Any], errors: list[str]) -> None:
    clause_id = "governing_law"
    approved_laws = _validate_text_list_field(clause, "approved_laws", clause_id, errors, required=True)
    preferred_law = _text(clause.get("preferred_law"))
    if preferred_law and approved_laws and preferred_law not in approved_laws:
        errors.append("Playbook clause governing_law preferred_law must be in approved_laws.")
    law_phrases = clause.get("law_phrases")
    if not isinstance(law_phrases, Mapping):
        errors.append("Playbook clause governing_law law_phrases must be an object.")
        return
    phrase_keys = [_text(key) for key in law_phrases.keys() if _text(key)]
    missing_phrases = [law for law in approved_laws if not _text(law_phrases.get(law))]
    if missing_phrases:
        errors.append("Playbook clause governing_law law_phrases missing: " + ", ".join(missing_phrases) + ".")
    extra_phrases = sorted(key for key in phrase_keys if key not in approved_laws)
    if extra_phrases:
        errors.append("Playbook clause governing_law law_phrases has unsupported key(s): " + ", ".join(extra_phrases) + ".")


def _validate_term_survival_policy_schema(clause: Mapping[str, Any], errors: list[str]) -> None:
    max_term_years = clause.get("max_term_years")
    if isinstance(max_term_years, bool) or not isinstance(max_term_years, int):
        errors.append("Playbook clause term_and_survival max_term_years must be an integer.")
    elif max_term_years < 1 or max_term_years > 25:
        errors.append("Playbook clause term_and_survival max_term_years must be between 1 and 25.")
    _validate_text_list_field(clause, "indefinite_terms", "term_and_survival", errors, required=True)
    _validate_text_list_field(clause, "longer_survival_carve_out_terms", "term_and_survival", errors, required=True)


def _validate_text_list_field(
    clause: Mapping[str, Any],
    field: str,
    clause_id: str,
    errors: list[str],
    *,
    required: bool,
) -> list[str]:
    value = clause.get(field)
    if value is None:
        if required:
            errors.append(f"Playbook clause {clause_id} must include {field}.")
        return []
    if not isinstance(value, list):
        errors.append(f"Playbook clause {clause_id} {field} must be a list.")
        return []
    items: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _text(item)
        if not text:
            errors.append(f"Playbook clause {clause_id} {field}[{index}] must be text.")
            continue
        normalized = text.lower()
        if normalized in seen:
            errors.append(f"Playbook clause {clause_id} {field} must not contain duplicate value {text}.")
            continue
        seen.add(normalized)
        items.append(text)
    if required and not items:
        errors.append(f"Playbook clause {clause_id} {field} must not be empty.")
    return items


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
    items: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            errors.append(f"Playbook clause {clause_id} rules.{field}[{index}] must be an object.")
            continue
        condition_id = _text(item.get("id"))
        if condition_id:
            normalized = condition_id.lower()
            if normalized in seen_ids:
                errors.append(f"Playbook clause {clause_id} rules.{field} must not contain duplicate id {condition_id}.")
            seen_ids.add(normalized)
        items.append(item)
    return items


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
    approved_laws = [_text(law) for law in clause.get("approved_laws", []) if _text(law)]
    option_values = [_text(option.get("value")) for option in options]
    if option_values != approved_laws:
        errors.append("Playbook clause governing_law rules.approved_options values must match approved_laws.")
    defaults = [option for option in options if option.get("default") is True]
    if len(defaults) != 1:
        errors.append("Playbook clause governing_law rules.approved_options must have exactly one default option.")
    elif approved_laws:
        preferred_law = _text(clause.get("preferred_law")) or approved_laws[0]
        if _text(defaults[0].get("value")) != preferred_law:
            errors.append("Playbook clause governing_law rules.approved_options default must match preferred_law.")


def _text(value: object) -> str:
    return str(value or "").strip()


def _int_value(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
