from __future__ import annotations

import re
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
    _year_count_label,
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

# A clause is reviewed either by native Python checks (the original six) or
# generically from its data definition by the AI-first engine. The marker is
# optional and defaults to "native" so existing playbooks are unaffected.
CLAUSE_ENGINE_NATIVE = "native"
CLAUSE_ENGINE_DYNAMIC = "dynamic"
CLAUSE_ENGINES = {CLAUSE_ENGINE_NATIVE, CLAUSE_ENGINE_DYNAMIC}

# The clause ids backed by Python checks. Dynamic clauses must NOT use these.
# non_circumvention was migrated to a pure dynamic Playbook clause (tracer), so
# it is intentionally absent even though its id remains well-known.
NATIVE_CLAUSE_IDS = {
    "mutuality",
    "confidential_information",
    "governing_law",
    "term_and_survival",
    "signatures",
}

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
    "engine",
}

# Fields a dynamic clause may carry beyond the core set. A dynamic clause type
# is fully self-describing in data: detection cues (core search_terms etc.),
# pass/review/fail criteria (rules), and the fallback/redline wording +
# clause-specific instructions below.
DYNAMIC_CLAUSE_EXTRA_FIELDS = {
    "fallback",
    "instructions",
}

# Allowed shape for a dynamic clause's fallback/redline wording block.
DYNAMIC_FALLBACK_FIELDS = {
    "redline_action",
    "wording",
    "approved_positions",
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
        "required_text_lists": ["indefinite_terms"],
        "optional_text_lists": ["longer_survival_carve_out_terms"],
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
    normalized_playbook = normalize_playbook_policy(playbook)
    clauses = normalized_playbook.get("clauses", [])
    return {
        "version": PLAYBOOK_RULES_VERSION,
        "assessment_schema": deepcopy(AI_CLAUSE_ASSESSMENT_SCHEMA),
        "clauses": [
            clause_rules_for_ai(clause)
            for clause in clauses
            if isinstance(clause, Mapping)
        ],
    }


def normalize_playbook_policy(playbook: Mapping[str, Any]) -> dict[str, Any]:
    validate_playbook_rules(playbook)
    normalized = deepcopy(dict(playbook))
    clauses = normalized.get("clauses", [])
    if isinstance(clauses, list):
        normalized["clauses"] = [
            normalize_clause_policy(clause) if isinstance(clause, Mapping) else deepcopy(clause)
            for clause in clauses
        ]
    return normalized


def normalize_clause_policy(clause: Mapping[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(dict(clause))
    clause_id = _text(normalized.get("id"))
    if clause_id == "governing_law":
        _normalize_governing_law_clause(normalized)
    elif clause_id == "term_and_survival":
        _normalize_term_survival_clause(normalized)
    return normalized


def clause_rules_for_ai(clause: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_clause_policy(clause)
    rules = normalized.get("rules")
    packet_clause = {
        "clause_id": str(normalized.get("id") or ""),
        "name": str(normalized.get("name") or ""),
        "type": str(normalized.get("type") or ""),
        "engine": clause_engine(normalized),
        "requirement": str(normalized.get("requirement") or ""),
        "preferred_position": str(normalized.get("preferred_position") or ""),
        "check_trigger": str(normalized.get("check_trigger") or ""),
        "acceptable_language": str(normalized.get("acceptable_language") or ""),
        "evidence_guidance": str(normalized.get("evidence_guidance") or ""),
        "semantic_signals": [
            str(signal)
            for signal in normalized.get("semantic_signals", [])
            if str(signal).strip()
        ],
        "rules": deepcopy(rules) if isinstance(rules, Mapping) else {},
    }
    # Dynamic clauses carry their fallback/redline wording and clause-specific
    # instructions in data; surface them so the AI packet is fully self-describing
    # for clause types the code has never seen.
    fallback = normalized.get("fallback")
    if isinstance(fallback, Mapping):
        packet_clause["fallback"] = deepcopy(dict(fallback))
    instructions = _clause_instructions(normalized)
    if instructions:
        packet_clause["instructions"] = instructions
    return packet_clause


def _clause_instructions(clause: Mapping[str, Any]) -> list[str]:
    raw = clause.get("instructions")
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, list):
        return [_text(item) for item in raw if _text(item)]
    return []


def _normalize_governing_law_clause(clause: dict[str, Any]) -> None:
    approved_laws = [_text(law) for law in clause.get("approved_laws", []) if _text(law)]
    if not approved_laws:
        return
    approved_label = _join_with_or(approved_laws)
    preferred_law = _text(clause.get("preferred_law"))
    if preferred_law not in approved_laws:
        preferred_law = approved_laws[0]

    clause["requirement"] = f"Governing law must be {approved_label}."
    clause["preferred_position"] = (
        "The governing law is one of the approved jurisdictions, preferably "
        f"{preferred_law} unless the matter context supports another approved option."
    )
    clause["check_trigger"] = (
        "The governing law is missing, unclear, or names a jurisdiction outside "
        f"{approved_label}."
    )
    clause["acceptable_language"] = (
        "This Agreement shall be governed by the laws of "
        + _join_with_or([_law_phrase(clause, law) for law in approved_laws])
        + "."
    )

    rules = _normalized_rules(clause)
    if not rules:
        return
    rules["acceptable_position"] = (
        "The governing law is one of the approved jurisdictions in the playbook, "
        f"with {preferred_law} as the preferred option unless matter context supports another approved jurisdiction."
    )
    _set_condition_description(
        rules.get("pass_conditions"),
        "approved_governing_law",
        f"The governing-law clause names {approved_label}.",
    )
    _set_condition_description(
        rules.get("fail_conditions"),
        "unapproved_governing_law",
        f"The governing-law clause names a jurisdiction outside {approved_label}.",
    )
    redline_guidance = rules.get("redline_guidance")
    if isinstance(redline_guidance, dict):
        redline_guidance["drafting_note"] = (
            "Use one of the approved jurisdiction options. Default to "
            f"{preferred_law} unless another approved option is selected."
        )
    rules["approved_options"] = [
        {
            "id": _option_id(law),
            "label": law,
            "value": law,
            "default": law == preferred_law,
        }
        for law in approved_laws
    ]


def _normalize_term_survival_clause(clause: dict[str, Any]) -> None:
    max_years = _int_value(clause.get("max_term_years")) or 5
    cap_label = _year_count_label(max_years)

    clause["requirement"] = (
        "The NDA term and ordinary confidentiality survival must be fixed at up to "
        f"{cap_label}."
    )
    clause["preferred_position"] = (
        "Ordinary confidentiality obligations survive for a fixed period of up to "
        f"{cap_label}. Narrow trade-secret, legal/regulatory, and data-protection obligations may survive "
        "for as long as the protected status or law requires."
    )
    clause["check_trigger"] = (
        "Ordinary confidentiality is perpetual, indefinite, relationship-based, tied to information remaining "
        f"confidential, longer than {cap_label}, or missing a clear fixed term or survival period."
    )
    clause["acceptable_language"] = (
        "The confidentiality obligations survive for a fixed period of up to "
        f"{cap_label}, except for trade secrets or legal obligations that require a longer period."
    )

    rules = _normalized_rules(clause)
    if not rules:
        return
    rules["acceptable_position"] = (
        "Ordinary confidentiality obligations have a fixed survival period of up to "
        f"{cap_label}, while narrow trade-secret, legal, regulatory, or data-protection carve-outs may survive longer."
    )
    _set_condition_description(
        rules.get("pass_conditions"),
        "fixed_survival_within_cap",
        f"The term or ordinary confidentiality survival period is fixed and does not exceed {cap_label}.",
    )
    _set_condition_description(
        rules.get("fail_conditions"),
        "ordinary_survival_exceeds_cap_or_is_indefinite",
        "Ordinary confidentiality survival is indefinite, perpetual, relationship-based, "
        f"tied to information remaining confidential, or longer than {cap_label}.",
    )
    redline_guidance = rules.get("redline_guidance")
    if isinstance(redline_guidance, dict):
        redline_guidance["drafting_note"] = (
            "Use the survival template with the playbook maximum term and narrow longer-survival carve-outs."
        )


def _normalized_rules(clause: dict[str, Any]) -> dict[str, Any] | None:
    rules = clause.get("rules")
    if not isinstance(rules, Mapping):
        return None
    copied = deepcopy(dict(rules))
    clause["rules"] = copied
    return copied


def _set_condition_description(conditions: object, condition_id: str, description: str) -> None:
    if not isinstance(conditions, list):
        return
    for condition in conditions:
        if isinstance(condition, dict) and _text(condition.get("id")) == condition_id:
            condition["description"] = description
            return


def _law_phrase(clause: Mapping[str, Any], law: str) -> str:
    law_phrases = clause.get("law_phrases")
    if isinstance(law_phrases, Mapping):
        phrase = _text(law_phrases.get(law))
        if phrase:
            return phrase
    return law


def _option_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "option"


def _join_with_or(values: Sequence[str]) -> str:
    cleaned = [_text(value) for value in values if _text(value)]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} or {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", or {cleaned[-1]}"


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


def clause_engine(clause: Mapping[str, Any]) -> str:
    # Returns the declared engine verbatim (default native). Callers validate it
    # against CLAUSE_ENGINES; an unknown value is surfaced as an error there.
    return _text(clause.get("engine")) or CLAUSE_ENGINE_NATIVE


def is_dynamic_clause(clause: Mapping[str, Any]) -> bool:
    return clause_engine(clause) == CLAUSE_ENGINE_DYNAMIC


def _validate_clause_policy_schema(clause: Mapping[str, Any], errors: list[str]) -> None:
    clause_id = _text(clause.get("id")) or "unknown"
    engine = clause_engine(clause)
    if engine not in CLAUSE_ENGINES:
        errors.append(f"Playbook clause {clause_id} engine must be one of {', '.join(sorted(CLAUSE_ENGINES))}.")

    if engine == CLAUSE_ENGINE_DYNAMIC:
        _validate_dynamic_clause_schema(clause, clause_id, errors)
    else:
        _validate_native_clause_schema(clause, clause_id, errors)


def _validate_native_clause_schema(clause: Mapping[str, Any], clause_id: str, errors: list[str]) -> None:
    if clause_id not in NATIVE_CLAUSE_IDS:
        errors.append(
            f"Playbook clause {clause_id} is not a known native clause; set engine to {CLAUSE_ENGINE_DYNAMIC} "
            "to define it as data."
        )
    allowed_fields = CORE_CLAUSE_FIELDS | CLAUSE_POLICY_FIELDS.get(clause_id, set())
    unknown_fields = sorted(str(field) for field in clause.keys() if str(field) not in allowed_fields)
    if clause_id in CLAUSE_POLICY_FIELDS and unknown_fields:
        errors.append(f"Playbook clause {clause_id} has unsupported field(s): {', '.join(unknown_fields)}.")

    _validate_clause_common_fields(clause, clause_id, errors)

    if clause_id == "governing_law":
        _validate_governing_law_policy_schema(clause, errors)
    elif clause_id == "term_and_survival":
        _validate_term_survival_policy_schema(clause, errors)


def _validate_dynamic_clause_schema(clause: Mapping[str, Any], clause_id: str, errors: list[str]) -> None:
    if clause_id in NATIVE_CLAUSE_IDS:
        errors.append(
            f"Playbook clause {clause_id} is a native clause id and cannot be redefined as a dynamic clause."
        )
    allowed_fields = CORE_CLAUSE_FIELDS | DYNAMIC_CLAUSE_EXTRA_FIELDS
    unknown_fields = sorted(str(field) for field in clause.keys() if str(field) not in allowed_fields)
    if unknown_fields:
        errors.append(f"Playbook clause {clause_id} has unsupported field(s): {', '.join(unknown_fields)}.")

    _validate_clause_common_fields(clause, clause_id, errors)
    _validate_dynamic_fallback(clause, clause_id, errors)
    _validate_dynamic_instructions(clause, clause_id, errors)


def _validate_clause_common_fields(clause: Mapping[str, Any], clause_id: str, errors: list[str]) -> None:
    for field in CORE_REQUIRED_TEXT_FIELDS:
        if not _text(clause.get(field)):
            errors.append(f"Playbook clause {clause_id} must include {field}.")
    if _text(clause.get("type")) not in {"required", "prohibited"}:
        errors.append(f"Playbook clause {clause_id} type must be required or prohibited.")
    _validate_text_list_field(clause, "search_terms", clause_id, errors, required=True)
    for field in sorted(CLAUSE_TEXT_LIST_FIELDS - {"search_terms"}):
        if field in clause:
            _validate_text_list_field(clause, field, clause_id, errors, required=False)


def _validate_dynamic_fallback(clause: Mapping[str, Any], clause_id: str, errors: list[str]) -> None:
    fallback = clause.get("fallback")
    if fallback is None:
        errors.append(f"Playbook clause {clause_id} must include fallback wording for dynamic findings.")
        return
    if not isinstance(fallback, Mapping):
        errors.append(f"Playbook clause {clause_id} fallback must be an object.")
        return
    unknown = sorted(str(key) for key in fallback.keys() if str(key) not in DYNAMIC_FALLBACK_FIELDS)
    if unknown:
        errors.append(f"Playbook clause {clause_id} fallback has unsupported field(s): {', '.join(unknown)}.")
    redline_action = _text(fallback.get("redline_action"))
    if redline_action not in AI_ASSESSMENT_REDLINE_ACTIONS:
        errors.append(f"Playbook clause {clause_id} fallback.redline_action is unsupported.")
    else:
        # The fallback action must be coherent with the clause type: a prohibited
        # clause is REMOVED (delete_paragraph / no_change), never have text added; a
        # required clause is FIXED (replace / insert / no_change), never deleted.
        clause_type = _text(clause.get("type"))
        if clause_type == "prohibited" and redline_action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_INSERT_AFTER_PARAGRAPH}:
            errors.append(
                f"Playbook clause {clause_id} is prohibited; fallback.redline_action {redline_action} adds text -- "
                "a prohibited clause should be removed (delete_paragraph) or left (no_change)."
            )
        elif clause_type == "required" and redline_action == REDLINE_DELETE_PARAGRAPH:
            errors.append(
                f"Playbook clause {clause_id} is required; fallback.redline_action delete_paragraph would remove "
                "required language (use replace, insert_after_paragraph, or no_change)."
            )
    # Only the text-inserting actions need wording; delete_paragraph and no_change do not.
    wording_required_actions = {REDLINE_REPLACE_PARAGRAPH, REDLINE_INSERT_AFTER_PARAGRAPH}
    if redline_action in wording_required_actions and not _text(fallback.get("wording")):
        errors.append(
            f"Playbook clause {clause_id} fallback.wording must be text when redline_action is {redline_action}."
        )
    if "approved_positions" in fallback:
        _validate_text_list_field(fallback, "approved_positions", clause_id, errors, required=False)


def _validate_dynamic_instructions(clause: Mapping[str, Any], clause_id: str, errors: list[str]) -> None:
    instructions = clause.get("instructions")
    if instructions is None:
        return
    if isinstance(instructions, str):
        if not instructions.strip():
            errors.append(f"Playbook clause {clause_id} instructions must not be blank.")
        return
    if isinstance(instructions, list):
        for index, item in enumerate(instructions):
            if not _text(item):
                errors.append(f"Playbook clause {clause_id} instructions[{index}] must be text.")
        return
    errors.append(f"Playbook clause {clause_id} instructions must be text or a list of text.")


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
    _validate_text_list_field(clause, "longer_survival_carve_out_terms", "term_and_survival", errors, required=False)


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
