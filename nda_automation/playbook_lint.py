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
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from .forum_shape import forum_shape_problem as _forum_shape_problem

__all__ = [
    "LintViolation",
    "lint_playbook",
    "CHECK_IDS",
    "CHECKS",
    "VALID_ISSUE_TYPES",
    "VALID_REDLINE_ACTIONS",
    "REDLINE_ACTIONS_NEEDING_TEMPLATE",
    "check_option_id_collision",
    "check_governing_law_forum_present",
    "check_trigger_terms_present",
    "check_condition_contradiction",
    "check_law_alias_collision",
    "has_printable_content",
]


# Unicode format/zero-width characters that carry NO printable content but a naive
# ``str.strip()`` leaves in place. A "term" or value built only from these is
# effectively blank -- it can never be matched in a document -- so the lint must
# treat it as empty rather than as a present-but-invisible value.
_ZERO_WIDTH_CHARS = frozenset({"​", "‌", "‍", "﻿", " "})
_WORD_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)


def has_printable_content(value: object) -> bool:
    """True when ``value`` has visible/word content after stripping zero-width chars.

    Strips Cf-category code points (zero-width space/joiner, BOM, ...) and the
    non-breaking space, then requires at least one ``\\w`` character to remain. An
    invisible-unicode-only string returns ``False`` so it can never pass as a
    present term.
    """
    text = str(value or "")
    kept = [
        ch
        for ch in text
        if ch not in _ZERO_WIDTH_CHARS and unicodedata.category(ch) != "Cf"
    ]
    return bool(_WORD_CHAR_PATTERN.search("".join(kept)))

# ---------------------------------------------------------------------------
# Vocabulary -- kept locally so the lint can run on a raw playbook mapping
# without importing the whole review engine. These mirror the canonical
# constants in ``ai_assessment_contract`` / ``redline_actions`` / ``checks.common``;
# a divergence test pins them so they cannot silently drift.
# ---------------------------------------------------------------------------

VALID_ISSUE_TYPES: frozenset[str] = frozenset(
    {"none", "missing", "present_but_wrong", "unclear"}
)
# The redline actions a playbook FALLBACK may name. These are the PARAGRAPH-level
# actions only (``AI_ASSESSMENT_PARAGRAPH_REDLINE_ACTIONS`` in the contract). The
# AI-only ``strike_span``/``replace_span`` sugar is deliberately NOT here: a
# playbook fallback has no span to anchor against, and span actions are lowered to
# ``replace_paragraph`` at parse time, so they are never a valid playbook action.
# ``test_local_vocabulary_matches_contract`` pins this exact relationship.
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


# A reference to an enumerated, pre-approved option set. We require an
# "enumerated set" qualifier to sit next to option/law/jurisdiction vocabulary
# so generic mentions of "lawful" or "the law" do not trip the check.
#
# Two distinct shapes are recognised:
#   * "approved"/"playbook" may qualify the broad option-set vocabulary,
#     including bare law/laws ("approved jurisdictions", "playbook-approved law").
#   * "permitted"/"listed" only qualify *enumerable list* nouns
#     (option/jurisdiction). They are deliberately NOT allowed to pair with the
#     bare "law"/"laws" noun, because "permitted/required by law" (and similar
#     lawful carve-out prose) is standard clause wording, not a reference to an
#     enumerated approved-option set.
_APPROVED_OPTION_PATTERN = re.compile(
    r"\b(?:approved|playbook)\b[^.]{0,60}?"
    r"\b(?:option|options|jurisdiction|jurisdictions|law|laws|governing law)\b",
    re.IGNORECASE,
)
_PERMITTED_OPTION_PATTERN = re.compile(
    r"\b(?:permitted|listed)\b[^.]{0,40}?"
    r"\b(?:option|options|jurisdiction|jurisdictions)\b",
    re.IGNORECASE,
)


def _prose_references_approved_options(clause: Mapping[str, Any]) -> bool:
    parts: list[str] = [_text(clause.get(field)) for field in PROSE_FIELDS]
    rules = _rules(clause)
    parts.append(_text(rules.get("acceptable_position")))
    for _field, condition in _all_conditions(rules):
        parts.append(_text(condition.get("description")))
    blob = " ".join(part for part in parts if part)
    return bool(
        _APPROVED_OPTION_PATTERN.search(blob)
        or _PERMITTED_OPTION_PATTERN.search(blob)
    )


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
# Check 6: option_id_collision
# ---------------------------------------------------------------------------

# Mirror of ``nda_automation.playbook_rules._option_id``: the short, internal id
# the engine derives from an option's name (e.g. "England and Wales" ->
# "england_and_wales"). Inlined so the lint stays standalone (no engine import);
# a divergence test pins the two so they cannot silently drift.
_OPTION_ID_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _derive_option_id(value: str) -> str:
    return _OPTION_ID_NON_ALNUM.sub("_", value.lower()).strip("_") or "option"


def _option_display_name(option: Any) -> str:
    """The human-facing name an option's id is *derived* from.

    The engine derives the option id from the option's ``value`` first, falling
    back to ``label`` -- see ``nda_automation.playbook_rules`` (``_option_id(value
    or label)`` at the input rules, and ``_option_id(law)`` where ``law`` is the
    ``value`` when the list is rebuilt from ``approved_laws``). We MUST mirror that
    value-first order so options whose *values* collide under distinct labels are
    caught. A plain string option (e.g. an ``approved_laws`` entry) is its own
    name. The explicit ``id`` is handled separately by the explicit-id check below.
    """

    if isinstance(option, Mapping):
        return _text(option.get("value")) or _text(option.get("label"))
    return _text(option)


def _option_explicit_id(option: Any) -> str:
    """The explicit ``id`` an option carries, if any.

    This is the ACTUAL downstream JOIN KEY: ``_approved_governing_law_options``
    (nda_automation/nda_generation.py) builds ``resolved[option["id"]] = value``
    keyed on the explicit id. Plain-string options carry no explicit id.
    """

    if isinstance(option, Mapping):
        return _text(option.get("id"))
    return ""


def check_option_id_collision(clause: Mapping[str, Any]) -> list[LintViolation]:
    """Approved options must resolve to DISTINCT option ids on every key path.

    The engine derives a short internal id from an option's *value* (see
    :func:`nda_automation.playbook_rules._option_id`), and the generation join
    (:func:`nda_automation.nda_generation._approved_governing_law_options`) keys a
    ``resolved[id] -> value`` map on each option's *explicit* ``id``. Both are
    JOIN KEYS, so a collision on either path silently shadows one option with
    another and selection/validation resolves to the WRONG option (e.g. the wrong
    jurisdiction). Two failure modes are flagged:

    1. NAME-DERIVED collision: two distinct option values (value-first, mirroring
       the engine) collapse to the same derived id -- e.g. they differ only by
       punctuation, case, or stripped characters.
    2. EXPLICIT-ID collision: two options carry the same explicit ``id`` (the
       generation join key) but map to different laws/values, so one drops out of
       the ``resolved`` map entirely.

    Only real collisions (2+ distinct options -> one key) are flagged.
    """

    clause_id = _clause_id(clause)
    violations: list[LintViolation] = []

    # (1) NAME-DERIVED collision: distinct values that derive one id.
    names_by_id: dict[str, list[str]] = {}
    seen_names: set[str] = set()
    for option in _approved_options(clause):
        name = _option_display_name(option)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        derived = _derive_option_id(name)
        names_by_id.setdefault(derived, []).append(name)

    for derived, names in names_by_id.items():
        if len(names) > 1:
            colliding = ", ".join(f"'{name}'" for name in sorted(names))
            violations.append(
                LintViolation(
                    clause_id,
                    "referential_integrity",
                    f"approved options {colliding} derive the same option id "
                    f"'{derived}', so one shadows the other within the clause -- "
                    "distinct options must have distinct ids",
                )
            )

    # (2) EXPLICIT-ID collision: 2+ options sharing one explicit id (the
    # generation join key). We map id -> the set of distinct values it would
    # resolve to; only flag when the same id maps to MORE THAN ONE distinct
    # value, i.e. one option genuinely shadows another (identical duplicate
    # rows would not change which value the join resolves to).
    values_by_explicit_id: dict[str, list[str]] = {}
    for option in _approved_options(clause):
        explicit_id = _option_explicit_id(option)
        if not explicit_id:
            continue
        value = _option_display_name(option)
        values_by_explicit_id.setdefault(explicit_id, []).append(value)

    for explicit_id, values in values_by_explicit_id.items():
        distinct_values = sorted({value for value in values if value})
        if len(values) > 1 and len(distinct_values) > 1:
            colliding = ", ".join(f"'{value}'" for value in distinct_values)
            violations.append(
                LintViolation(
                    clause_id,
                    "referential_integrity",
                    f"approved options {colliding} share the explicit id "
                    f"'{explicit_id}', which is the generation join key, so one "
                    "silently drops out of the resolved option map -- distinct "
                    "options must have distinct explicit ids",
                )
            )

    return violations


# ---------------------------------------------------------------------------
# Check 7: governing_law_forum_present
# ---------------------------------------------------------------------------

_GOVERNING_LAW_CLAUSE_ID = "governing_law"


def check_governing_law_forum_present(clause: Mapping[str, Any]) -> list[LintViolation]:
    """Every governing-law approved option must name a non-empty court/forum.

    The ``forum_jurisdiction`` string pairs each approved governing law with the
    venue whose courts have authority. It is the single source the AI law<->forum
    recognition check (``law_forum_check``) reads to flag a law/forum mismatch, and
    the value generation falls back to when no signing-entity registers a court for
    the option. An approved law with NO authored forum means: the review engine
    cannot recognise/verify that law's forum, and a generated NDA under that law
    has no court to write -- so publishing a governing law with an empty forum is a
    self-contradiction the gate must reject (you can't publish a law with no court).

    Only applies to the ``governing_law`` clause; every other clause is a no-op.
    """

    if _clause_id(clause) != _GOVERNING_LAW_CLAUSE_ID:
        return []

    rules = _rules(clause)
    raw_options = rules.get("approved_options")
    if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
        # The approved_options shape itself is enforced by the rules validator /
        # approved_options_present; nothing to forum-check here.
        return []

    violations: list[LintViolation] = []
    for index, option in enumerate(raw_options):
        if not isinstance(option, Mapping):
            continue
        label = (
            _text(option.get("label"))
            or _text(option.get("value"))
            or _text(option.get("id"))
            or f"<approved_options[{index}]>"
        )
        forum = _text(option.get("forum_jurisdiction"))
        if not forum:
            violations.append(
                LintViolation(
                    _GOVERNING_LAW_CLAUSE_ID,
                    "governing_law_forum_present",
                    f"approved governing law '{label}' has no forum_jurisdiction "
                    "(court/forum); a governing law cannot be published without a "
                    "court -- the review forum check and generation have nothing to "
                    "pair the law with",
                )
            )
            continue
        # Court-SHAPE: the forum_jurisdiction must look like a real court/venue.
        # The same screen the generation gate uses, so a non-court venue ("the
        # moon", "arbitration in Narnia"), a template placeholder, an injected
        # control phrase, or an oversized string is rejected at PUBLISH -- before it
        # can ever reach a signed NDA -- rather than only at generation time.
        problem = _forum_shape_problem(forum)
        if problem is not None:
            violations.append(
                LintViolation(
                    _GOVERNING_LAW_CLAUSE_ID,
                    "governing_law_forum_present",
                    f"approved governing law '{label}' has forum_jurisdiction "
                    f"'{forum}' which is not a valid court/venue: {problem}. A "
                    "non-court venue cannot be published into the law/forum pairing",
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Check 8: trigger_terms_present
# ---------------------------------------------------------------------------


def check_trigger_terms_present(clause: Mapping[str, Any]) -> list[LintViolation]:
    """Every clause must carry at least one non-blank search term.

    ``search_terms`` is the detection cue the deterministic checkers and the AI
    detector use to surface a clause to the review engine -- ``mutuality.py``,
    ``confidential_information.py``, and ``term_and_survival.py`` all read it, and
    a dynamic clause is only ever surfaced through its terms. A clause with no
    search term can never be located in a document, so its rules never fire: it is
    silently dead. The schema validator already requires this for a well-formed
    publish, but the publish lint enforces it independently so the trigger-term
    editor (now live for every clause) cannot ship a clause that nothing detects.

    ``semantic_signals`` is optional, so its absence is never flagged.
    """

    clause_id = _clause_id(clause)
    raw = clause.get("search_terms")
    terms: list[str] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        # A term is "present" only if it has printable/word content AFTER stripping
        # unicode format/zero-width characters. A zero-width-only "term" (e.g.
        # "​​") survives ``str.strip()`` and so would look present, but it
        # can never be matched in a document -- the clause would be silently dead. We
        # reject it the same as a blank term.
        terms = [_text(term) for term in raw if has_printable_content(term)]
    if terms:
        return []
    return [
        LintViolation(
            clause_id,
            "trigger_terms_present",
            "clause has no search_terms with printable content (after stripping "
            "zero-width / format characters), so the detector can never surface it "
            "and its review rules never fire -- add at least one real trigger term",
        )
    ]


# ---------------------------------------------------------------------------
# Check 9: condition_contradiction
# ---------------------------------------------------------------------------

# Normalises a condition description to a comparable key: lowercase, collapse
# whitespace, drop surrounding punctuation. Two conditions whose descriptions
# normalise identically describe the SAME triggering state.
_DESC_WS = re.compile(r"\s+")
_DESC_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$")


def _normalize_description(value: object) -> str:
    text = _DESC_WS.sub(" ", str(value or "").strip().lower())
    return _DESC_EDGE_PUNCT.sub("", text)


def check_condition_contradiction(clause: Mapping[str, Any]) -> list[LintViolation]:
    """Reject a clause whose conditions assert the SAME state is both pass and fail.

    A condition's ``description`` names the document state that triggers its
    decision. If the very same state (same normalised description) appears as a
    ``pass_condition`` AND as a ``fail_condition`` (or ``review_trigger``), the rule
    set is self-contradictory: the engine is told the identical situation both
    passes and is flagged, so the clause's verdict is undefined / order-dependent.

    This catches the CLEAR contradiction the spec calls for -- the same trigger
    described as both pass and fail -- deterministically, without judging meaning
    (Layer 2's job). Two conditions that merely share an id are already caught by
    ``condition_well_formed``; here we compare the human-described TRIGGER.
    """

    rules = _rules(clause)
    clause_id = _clause_id(clause)

    pass_descs: dict[str, str] = {}
    for condition in _condition_list(rules, "pass_conditions"):
        key = _normalize_description(condition.get("description"))
        if key:
            pass_descs.setdefault(key, _text(condition.get("description")))

    if not pass_descs:
        return []

    violations: list[LintViolation] = []
    seen: set[str] = set()
    for field in ("fail_conditions", "review_triggers"):
        for condition in _condition_list(rules, field):
            key = _normalize_description(condition.get("description"))
            if key and key in pass_descs and key not in seen:
                seen.add(key)
                described = pass_descs[key]
                violations.append(
                    LintViolation(
                        clause_id,
                        "condition_contradiction",
                        f"the same state '{described}' is described as BOTH a "
                        f"pass_condition and a {field[:-1] if field.endswith('s') else field} "
                        "-- the rules contradict themselves on whether this state "
                        "passes or is flagged",
                    )
                )
    return violations


# ---------------------------------------------------------------------------
# Check 10: law_alias_collision
# ---------------------------------------------------------------------------


def _law_aliases(option: Any) -> list[str]:
    """The recognition aliases an approved governing-law option carries.

    An option may name aliases under ``aliases`` / ``law_phrases`` / ``synonyms``
    (a list of strings) plus its own ``label``/``value`` as implicit aliases. These
    are the strings the review-side recognition matches a document's governing law
    against, so two approved laws sharing an alias are ambiguous: a document naming
    that alias resolves to whichever law happens to be checked first.
    """

    if not isinstance(option, Mapping):
        return []
    aliases: list[str] = []
    for key in ("aliases", "law_phrases", "synonyms"):
        raw = option.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            aliases.extend(_text(item) for item in raw if _text(item))
        elif isinstance(raw, str) and _text(raw):
            aliases.append(_text(raw))
    return aliases


def check_law_alias_collision(clause: Mapping[str, Any]) -> list[LintViolation]:
    """Warn when two approved governing laws share a recognition alias.

    Only applies to the ``governing_law`` clause. Each approved option's aliases
    (``aliases``/``law_phrases``/``synonyms``) are the strings the review side
    matches a document against to recognise its governing law. If two DISTINCT
    approved laws claim the same alias, recognition is ambiguous -- a document naming
    that alias could resolve to either law. We flag the collision so the author
    disambiguates before publishing.
    """

    if _clause_id(clause) != _GOVERNING_LAW_CLAUSE_ID:
        return []

    rules = _rules(clause)
    raw_options = rules.get("approved_options")
    if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
        return []

    # alias (normalised) -> set of distinct option labels that claim it.
    owners_by_alias: dict[str, dict[str, str]] = {}
    for option in raw_options:
        if not isinstance(option, Mapping):
            continue
        label = (
            _text(option.get("label"))
            or _text(option.get("value"))
            or _text(option.get("id"))
        )
        if not label:
            continue
        option_key = label.lower()
        for alias in _law_aliases(option):
            norm = _normalize_description(alias)
            if not norm:
                continue
            owners_by_alias.setdefault(norm, {})[option_key] = label

    violations: list[LintViolation] = []
    for norm, owners in owners_by_alias.items():
        if len(owners) > 1:
            colliding = ", ".join(f"'{name}'" for name in sorted(owners.values()))
            violations.append(
                LintViolation(
                    _GOVERNING_LAW_CLAUSE_ID,
                    "law_alias_collision",
                    f"approved governing laws {colliding} share the recognition alias "
                    f"'{norm}', so a document naming it resolves ambiguously -- "
                    "distinct laws must not share an alias",
                    severity="warning",
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
    "option_id_collision": check_option_id_collision,
    "governing_law_forum_present": check_governing_law_forum_present,
    "trigger_terms_present": check_trigger_terms_present,
    "condition_contradiction": check_condition_contradiction,
    "law_alias_collision": check_law_alias_collision,
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
        for check_id, check in CHECKS.items():
            # Per-check isolation: a single check throwing on an unusual-but-legal
            # clause must NOT abort the loop and silently disable every OTHER
            # check (which would turn the hard publish gate into a no-op). Instead
            # the failure is surfaced as a BLOCKING violation, so the gate
            # fails-closed and a self-contradictory playbook cannot slip through on
            # the back of one buggy check.
            try:
                violations.extend(check(clause))
            except Exception as exc:  # noqa: BLE001 - isolate one check's crash
                violations.append(
                    LintViolation(
                        _clause_id(clause),
                        check_id,
                        f"lint check '{check_id}' raised and could not validate this "
                        f"clause ({type(exc).__name__}: {exc}); blocking publish so a "
                        "broken check cannot silently pass an unchecked playbook",
                    )
                )
    return violations
