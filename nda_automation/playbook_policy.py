"""Derive the binding-policy block for the AI reviewer FROM the playbook.

North star: the policy block is DERIVED, never hardcoded. ``playbook.json`` is the
single source of truth for every rule, threshold, and approved option; this module
reads the already-faithful normalization (``playbook_rules.playbook_rules_for_ai`` for
the model-facing clause text + approved options, and ``normalize_playbook_policy`` for
the raw rule fields the AI packet drops) and renders the 5-rule policy prose from it.

If ``playbook.json`` changes (e.g. ``term_and_survival.max_term_years`` or
``governing_law.approved_laws``), the rendered block changes WITH it — proven by the
mutated-playbook test. The validated golden target prose lives at
``/tmp/catA-policy/policy_block.txt``; the golden-fact test asserts the rendered block
carries each rule's load-bearing facts.

CAVEAT (documented in RULE 1, per design section G): the list of prohibited restraint
categories is GENERATED from ``non_circumvention.prohibited_position_patterns`` labels.
A restraint with no matching pattern is not named in the policy. This is intentional —
the policy is exactly as complete as the playbook's rule list, so adding a pattern is
the ONLY way to extend coverage. A test asserts the generated list equals the playbook's
label set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .playbook_rules import (
    is_dynamic_clause,
    normalize_playbook_policy,
    playbook_rules_for_ai,
)
from .untrusted_text import neutralize_untrusted_text

# Length caps on AUTHORED free-text fields before they enter the AI packet, so a
# user-authored clause cannot blow the prompt budget. These bound the per-field
# contribution; the values are generous enough for legitimate authored prose.
AUTHORED_NAME_MAX_CHARS = 200
AUTHORED_LONG_TEXT_MAX_CHARS = 2000

# Dynamic clause ids ALREADY rendered as one of the five built-in rules above, so
# they are not re-emitted in the "ADDITIONAL AUTHORED CLAUSE RULES" section. Only
# non_circumvention is a built-in dynamic clause today (it drives RULE 1 + RULE 2).
_BUILTIN_RENDERED_CLAUSE_IDS = frozenset({"non_circumvention"})

# Remedy verb phrasing per fail-condition redline action, so the authored rule
# tells the model the prescribed fix exactly as the playbook condition encodes it.
_REDLINE_ACTION_REMEDY: dict[str, str] = {
    "delete_paragraph": "STRIKE/DELETE the offending clause in full",
    "replace_paragraph": "REPLACE the offending clause with compliant language",
    "insert_after_paragraph": "INSERT the required compliant language",
    "no_change": "flag for human review (no automatic edit)",
}

# Human-readable description for each prohibited_position_patterns label. The labels are
# the playbook's stable machine identifiers; this map renders them as the restraint
# categories the model reads. A label with no entry here still appears (rendered from
# its raw label) so a newly-added pattern is never silently dropped from the policy.
_RESTRAINT_LABEL_DESCRIPTIONS: dict[str, str] = {
    "non_compete": (
        "non-compete / agreements not to compete or engage in competing business"
    ),
    "non_solicit": (
        "non-solicitation / no-hire / no-poach of the other party's employees, "
        "consultants, contractors, customers, or suppliers — INCLUDING "
        '"introduced-party" / "became known to it" restraints'
    ),
    "non_circumvention": (
        "non-circumvention / no-direct-dealing / no-bypass / introduced-party dealing "
        "restrictions"
    ),
    "exclusivity": (
        "substitute-purpose or exclusivity / sole-and-exclusive / exclusive-dealing "
        "obligations"
    ),
    "ip_assignment": (
        'IP assignment ("hereby assigns", "all right, title and interest in ...")'
    ),
    "auto_renew_lock": (
        'auto-renewal locks, evergreen terms, or "may not terminate" / no-termination '
        "locks"
    ),
}

# The penalty restraint is rendered as its own RULE 2, so it is excluded from the
# RULE 1 restraint catalogue. perpetual_confidentiality is governed by RULE 3 (the
# survival cap), so it is likewise not a RULE 1 catch-all restraint.
_RULE1_EXCLUDED_LABELS = frozenset({"penalty", "perpetual_confidentiality"})


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _authored_text(value: object, max_chars: int) -> str:
    """Neutralize + length-cap an AUTHORED free-text field for the binding policy.

    Authored clause text is attacker-controllable, yet it enters the highest-trust
    "treat each as binding" section of the policy block. Route it through the shared
    neutralizer (strip control chars, defang line-start role markers) and bound its
    length so it cannot impersonate an instruction block or exhaust the prompt budget.
    """
    return neutralize_untrusted_text(value, max_chars=max_chars).strip()


def _clause_by_id(clauses: Sequence[Mapping[str, Any]], clause_id: str) -> Mapping[str, Any]:
    for clause in clauses:
        if isinstance(clause, Mapping) and _text(clause.get("id")) == clause_id:
            return clause
    return {}


def _packet_clause_by_id(
    clauses: Sequence[Mapping[str, Any]], clause_id: str
) -> Mapping[str, Any]:
    for clause in clauses:
        if isinstance(clause, Mapping) and _text(clause.get("clause_id")) == clause_id:
            return clause
    return {}


def _join_with_or(values: Sequence[str]) -> str:
    items = [value for value in values if value]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + ", or " + items[-1]


def prohibited_restraint_labels(playbook: Mapping[str, Any]) -> list[str]:
    """The non_circumvention prohibited_position_patterns labels, in playbook order.

    Exposed so a test can assert the RULE 1 restraint catalogue equals exactly this set
    (the section-G caveat: policy completeness == playbook rule-list completeness)."""
    normalized = normalize_playbook_policy(playbook)
    non_circ = _clause_by_id(normalized.get("clauses", []), "non_circumvention")
    patterns = non_circ.get("prohibited_position_patterns")
    if not isinstance(patterns, Sequence):
        return []
    return [
        _text(pattern.get("label"))
        for pattern in patterns
        if isinstance(pattern, Mapping) and _text(pattern.get("label"))
    ]


def _rule1_restraint_lines(labels: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for label in labels:
        if label in _RULE1_EXCLUDED_LABELS:
            continue
        description = _RESTRAINT_LABEL_DESCRIPTIONS.get(label, label.replace("_", " "))
        lines.append(f"  - {description};")
    return lines


def _has_label(labels: Sequence[str], label: str) -> bool:
    return label in set(labels)


def _fail_remedy_phrase(rules: Mapping[str, Any]) -> str:
    """The prescribed remedy for an authored clause, read from its fail conditions.

    Prefers the first fail condition's redline_action; falls back to the
    rules.redline_guidance.default_action. Returns "" when neither is present."""

    actions: list[str] = []
    fail_conditions = rules.get("fail_conditions")
    if isinstance(fail_conditions, Sequence):
        for condition in fail_conditions:
            if isinstance(condition, Mapping):
                action = _text(condition.get("redline_action"))
                if action:
                    actions.append(action)
    if not actions:
        guidance = rules.get("redline_guidance")
        if isinstance(guidance, Mapping):
            action = _text(guidance.get("default_action"))
            if action:
                actions.append(action)
    for action in actions:
        phrase = _REDLINE_ACTION_REMEDY.get(action)
        if phrase:
            return phrase
    return ""


def _dynamic_clause_rule_lines(
    normalized_clauses: Sequence[Mapping[str, Any]],
    packet_clauses: Sequence[Mapping[str, Any]],
) -> list[str]:
    """First-class binding-rule lines for every AUTHORED (dynamic) clause.

    A clause a user adds in the Playbook editor is a dynamic clause. The five rules
    above only cover the built-in ids, so without this an authored clause would be a
    "second-class citizen" — present in the packet's clause list but absent from the
    authoritative binding-policy block. This renders each dynamic clause (other than
    the built-in non_circumvention, already RULE 1/2) as its own binding rule,
    DERIVED entirely from its data: stance, requirement, acceptable position, and the
    prescribed remedy from its fail conditions. Nothing is hardcoded — add a dynamic
    clause and a new rule appears here automatically."""

    lines: list[str] = []
    for clause in normalized_clauses:
        if not isinstance(clause, Mapping) or not is_dynamic_clause(clause):
            continue
        clause_id = _text(clause.get("id"))
        if not clause_id or clause_id in _BUILTIN_RENDERED_CLAUSE_IDS:
            continue
        packet = _packet_clause_by_id(packet_clauses, clause_id)
        # AUTHORED free-text fields are attacker-controllable (a user can author any
        # clause text in the Playbook editor, or smuggle it via a direct-API publish)
        # and they land in the highest-trust "treat each as binding" section of the
        # policy block. Neutralize each so an authored payload cannot pose as a new
        # role/turn ("System:", "Assistant:") or smuggle control characters, and cap
        # the length so it cannot blow the prompt budget.
        name = (
            _authored_text(packet.get("name"), AUTHORED_NAME_MAX_CHARS)
            or _authored_text(clause.get("name"), AUTHORED_NAME_MAX_CHARS)
            or clause_id
        )
        stance = _text(packet.get("type")) or _text(clause.get("type"))
        stance_label = "PROHIBITED" if stance == "prohibited" else "REQUIRED"
        requirement = _authored_text(
            packet.get("requirement"), AUTHORED_LONG_TEXT_MAX_CHARS
        ) or _authored_text(clause.get("requirement"), AUTHORED_LONG_TEXT_MAX_CHARS)
        rules = clause.get("rules") if isinstance(clause.get("rules"), Mapping) else {}
        acceptable = _authored_text(
            rules.get("acceptable_position"), AUTHORED_LONG_TEXT_MAX_CHARS
        ) or _authored_text(
            packet.get("acceptable_language"), AUTHORED_LONG_TEXT_MAX_CHARS
        )
        remedy = _fail_remedy_phrase(rules)

        header = f"  - {name} [{stance_label}] (playbook clause `{clause_id}`):"
        lines.append(header)
        if requirement:
            lines.append(f"      Requirement: {requirement}")
        if acceptable:
            lines.append(f"      Acceptable position: {acceptable}")
        if remedy:
            lines.append(f"      If violated: {remedy}.")
    return lines


def build_playbook_policy_block(playbook: Mapping[str, Any]) -> str:
    """Render the 5-rule binding-policy block DERIVED from ``playbook``.

    Reads the faithful normalization (``playbook_rules_for_ai`` for the model-facing
    clause text + approved options; ``normalize_playbook_policy`` for the raw rule
    fields) and renders the policy prose. Nothing is hardcoded: the 5-year cap, the
    approved laws, the prohibited-restraint catalogue, and the carve-outs are all read
    from the playbook, so the block follows the playbook when it changes.
    """
    packet = playbook_rules_for_ai(playbook)
    normalized = normalize_playbook_policy(playbook)
    packet_clauses = packet.get("clauses", [])
    normalized_clauses = normalized.get("clauses", [])

    version = _text(normalized.get("version")) or _text(playbook.get("version"))
    name = _text(normalized.get("name")) or _text(playbook.get("name")) or "company NDA"

    # ---- RULE 1 + RULE 2: prohibited restraints + penalties (non_circumvention) ----
    labels = prohibited_restraint_labels(playbook)
    non_circ_norm = _clause_by_id(normalized_clauses, "non_circumvention")
    fallback = non_circ_norm.get("fallback")
    fallback_action = (
        _text(fallback.get("redline_action")) if isinstance(fallback, Mapping) else ""
    )
    drafting_note = ""
    rules = non_circ_norm.get("rules")
    if isinstance(rules, Mapping):
        redline_guidance = rules.get("redline_guidance")
        if isinstance(redline_guidance, Mapping):
            drafting_note = _text(redline_guidance.get("drafting_note"))
    restraint_lines = _rule1_restraint_lines(labels)
    has_penalty = _has_label(labels, "penalty")

    # ---- RULE 3: survival cap (term_and_survival) ----
    term_norm = _clause_by_id(normalized_clauses, "term_and_survival")
    max_term_years = term_norm.get("max_term_years")
    try:
        max_term_years = int(max_term_years)
    except (TypeError, ValueError):
        max_term_years = 5
    term_packet = _packet_clause_by_id(packet_clauses, "term_and_survival")
    # The packet requirement text already renders the cap in words ("five years"); reuse
    # the faithful normalized wording so the cap phrasing tracks the playbook exactly.
    term_requirement = _text(term_packet.get("requirement"))
    carve_outs = term_norm.get("longer_survival_carve_out_terms")
    indefinite_terms = term_norm.get("indefinite_terms")
    indefinite_examples = ""
    if isinstance(indefinite_terms, Sequence):
        examples = [_text(item) for item in indefinite_terms if _text(item)]
        indefinite_examples = ", ".join(f'"{item}"' for item in examples[:5])

    # ---- RULE 4: governing law + forum alignment (governing_law) ----
    gov_packet = _packet_clause_by_id(packet_clauses, "governing_law")
    approved_options: list[str] = []
    preferred_law = ""
    gov_rules = gov_packet.get("rules")
    if isinstance(gov_rules, Mapping):
        options = gov_rules.get("approved_options")
        if isinstance(options, Sequence):
            for option in options:
                if not isinstance(option, Mapping):
                    continue
                value = _text(option.get("value")) or _text(option.get("label"))
                if value:
                    approved_options.append(value)
                if option.get("default") and not preferred_law:
                    preferred_law = value
    if not preferred_law:
        gov_norm = _clause_by_id(normalized_clauses, "governing_law")
        preferred_law = _text(gov_norm.get("preferred_law"))
    approved_laws_label = _join_with_or(approved_options)

    # ---- RULE 5: no subordination / no carve-out negation (confidential_information) --
    ci_norm = _clause_by_id(normalized_clauses, "confidential_information")
    allowed_exclusions = ci_norm.get("allowed_exclusions")
    exclusion_descriptions = {
        "public_domain": "public domain",
        "prior_possession": "prior possession",
        "lawful_third_party_source": "lawful third-party source",
        "independent_development_without_use": "independent development",
    }
    exclusion_labels: list[str] = []
    if isinstance(allowed_exclusions, Sequence):
        for item in allowed_exclusions:
            key = _text(item)
            exclusion_labels.append(
                exclusion_descriptions.get(key, key.replace("_", " "))
            )
    exclusions_label = " / ".join(exclusion_labels)

    # -------------------------------------------------------------------- render
    lines: list[str] = []
    header_version = f"playbook v{version}" if version else "company playbook"
    lines.append(f"BINDING PLAYBOOK RULES ({name}, {header_version})")
    lines.append("")
    lines.append(
        "You are reviewing this NDA against a fixed company playbook. The following are "
        "the company's FIRM, NON-NEGOTIABLE positions. They are RULES, not preferences. "
        "Where a clause violates one of these rules, apply the prescribed remedy EXACTLY "
        "— in particular, where a rule says STRIKE/DELETE, you must remove the offending "
        "language outright. Do NOT narrow, soften, qualify, time-limit, or reword a "
        "prohibited restraint into an \"enforceable\" or \"reasonable\" version. Striking "
        "it IS the correct fix."
    )
    lines.append("")

    # RULE 1
    deletes_in_full = fallback_action == "delete_paragraph"
    remedy_phrase = "DELETED in full" if deletes_in_full else "removed"
    lines.append("RULE 1 — PROHIBITED BUSINESS RESTRAINTS MUST BE STRUCK, NOT NARROWED.")
    lines.append(
        "An NDA must not impose any commercial restraint beyond confidentiality. The "
        f"following are PROHIBITED and any operative instance must be {remedy_phrase} "
        "(remove the offending sentence/clause; do not replace it with a milder "
        "restraint):"
    )
    lines.extend(restraint_lines)
    if drafting_note:
        lines.append(
            f'The playbook\'s drafting note is explicit: "{drafting_note}" '
            "The exact verb is not controlling — fail and strike ANY clear business "
            "restraint beyond confidentiality, even when it sits next to "
            "freedom-preserving or ordinary-confidentiality language. Ordinary "
            "confidentiality / non-use language around it may remain; only the restraint "
            "is deleted."
        )
    lines.append(
        "(The restraint categories above are GENERATED from the playbook clause "
        "`non_circumvention` prohibited_position_patterns; a restraint with no matching "
        "pattern is not named here, so the policy is exactly as complete as the "
        "playbook's rule list. Source: requirement, prohibited_position_patterns, "
        "rules.fail_conditions, redline_guidance.drafting_note "
        f'"{fallback_action or "delete_paragraph"}".)'
    )
    lines.append("")

    # RULE 2
    if has_penalty:
        lines.append("RULE 2 — PENALTIES / LIQUIDATED DAMAGES MUST BE STRUCK.")
        lines.append(
            "Any penalty, liquidated-damages, punitive-damages, or \"per breach without "
            "proof of actual loss\" provision is PROHIBITED and must be DELETED. Do not "
            "convert it into a \"genuine pre-estimate of loss\" or otherwise rehabilitate "
            "it. Permitted remedies are injunctive/equitable relief and actual damages "
            "that must be proven."
        )
        lines.append(
            "(Source: playbook clause `non_circumvention` — `penalty` prohibited pattern "
            "+ rules.acceptable_position naming \"liquidated/punitive damages\"; "
            "remedy = delete.)"
        )
        lines.append("")

    # RULE 3
    cap_words = _cap_phrase(term_requirement, max_term_years)
    lines.append(
        f"RULE 3 — ORDINARY CONFIDENTIALITY SURVIVAL IS CAPPED AT {cap_words.upper()}."
    )
    lines.append(
        "The agreement term and ordinary confidentiality/non-use survival must be a "
        f"FIXED period of at most {cap_words}. Indefinite, perpetual"
        + (f" ({indefinite_examples})" if indefinite_examples else "")
        + ", value-based, or relationship-based survival applied to ordinary "
        "Confidential Information is a FAIL and must be REPLACED with a fixed period of "
        f"up to {max_term_years} year{'s' if max_term_years != 1 else ''}. This applies "
        "wherever the perpetual/indefinite survival is set, INCLUDING when it is folded "
        "into the definition of Confidential Information or cross-referenced from the "
        "term clause. Longer or indefinite survival is permitted ONLY where it is "
        "expressly scoped to one of these carve-out categories: "
        + (_carve_out_label(carve_outs) or "trade secrets, legal/regulatory obligations, "
           "or personal data / data-protection obligations")
        + ". Preserve any such legitimate carve-out."
    )
    lines.append(
        "(Source: playbook clause `term_and_survival` — max_term_years="
        f"{max_term_years}, requirement, preferred_position, indefinite_terms, "
        "longer_survival_carve_out_terms, rules.fail_conditions "
        '"ordinary_survival_exceeds_cap_or_is_indefinite", redline_template.)'
    )
    lines.append("")

    # RULE 4
    lines.append(
        "RULE 4 — GOVERNING LAW MUST BE APPROVED, AND THE FORUM/VENUE MUST MATCH THE "
        "GOVERNING LAW."
    )
    preferred_clause = (
        f" ({preferred_law} is the default/preferred)" if preferred_law else ""
    )
    lines.append(
        "The operative governing law must be one of the approved jurisdictions: "
        f"{approved_laws_label}{preferred_clause}. A clearly-named jurisdiction outside "
        "that list is a FAIL. Separately, the dispute forum / venue / "
        "exclusive-jurisdiction / arbitration seat must name the SAME jurisdiction as "
        "the governing law: a split where the governing law is one approved jurisdiction "
        "but the exclusive forum or arbitration seat is a DIFFERENT (or unapproved) "
        "jurisdiction is a defect that must be flagged and reconciled. The correct fix "
        "is to ALIGN THE FORUM TO THE APPROVED GOVERNING LAW (keep the approved law; "
        "move the courts/seat to that same jurisdiction) — do NOT fix a mismatch by "
        "switching the governing law to an unapproved forum's jurisdiction."
    )
    lines.append(
        "(Source: playbook clause `governing_law` — approved_laws, preferred_law "
        f'"{preferred_law}", rules.fail_conditions "unapproved_governing_law"; '
        "conditional/secondary-governing-law override handling.)"
    )
    lines.append("")

    # RULE 5
    lines.append(
        "RULE 5 — THE NDA MUST NOT BE SUBORDINATED TO AN UNSEEN AGREEMENT, AND ITS "
        "STANDARD CARVE-OUTS MUST NOT BE NEGATED."
    )
    lines.append(
        "  (a) Subordination: language that makes the NDA \"subject to\" an external, "
        "unreviewed agreement (e.g. a Master Services Agreement) and provides that the "
        "external agreement \"shall prevail in the event of any conflict\" silently "
        "overrides the NDA's confidentiality protections and must be STRUCK/neutralised "
        "(at minimum the \"shall prevail\" override must be removed) so the NDA's terms "
        "are not subordinated to an unseen document."
    )
    standard_exclusions = exclusions_label or (
        "public domain / prior possession / lawful third-party source / independent "
        "development"
    )
    lines.append(
        "  (b) Carve-out negation: a \"notwithstanding ... the standard exclusions shall "
        "not apply\" sentence that lets the Disclosing Party unilaterally switch off the "
        f"standard Confidential Information exclusions ({standard_exclusions}) must be "
        "STRUCK so the standard exclusions remain effective. Do NOT delete the standard "
        "exclusions list itself — delete only the negating sentence."
    )
    lines.append(
        "(Source: playbook clause `governing_law` conditional-override handling; clause "
        "`confidential_information` — allowed_exclusions, requirement that the definition "
        "keep only the standard carve-outs and that they remain operative.)"
    )
    lines.append("")

    # ---- ADDITIONAL AUTHORED CLAUSE RULES (any dynamic clause beyond the built-ins) --
    # The five rules above are rendered from the built-in clause ids. A clause a user
    # AUTHORS in the Playbook editor is a DYNAMIC clause (engine="dynamic"); it must
    # NOT be a second-class citizen. Render each such clause as its own first-class
    # binding rule, derived entirely from its data, so the model treats an authored
    # clause's position as firmly as the built-in ones.
    extra_rule_lines = _dynamic_clause_rule_lines(normalized_clauses, packet_clauses)
    if extra_rule_lines:
        lines.append(
            "ADDITIONAL AUTHORED CLAUSE RULES (authored in the Playbook; treat each as "
            "binding):"
        )
        lines.extend(extra_rule_lines)
        lines.append("")

    # SCOPE
    lines.append("SCOPE INSTRUCTION (MANDATORY).")
    lines.append(
        "Edit ONLY defective language that one of the rules above actually touches. Do "
        "NOT rewrite, \"improve\", or restyle clauses the rules do not reach. Sound "
        "boilerplate (recitals, mutual obligations, permitted/compelled disclosures, "
        "data protection, return/destruction, no-warranty, no-licence, "
        "IP-ownership-as-distinct-from-assignment, severability, general boilerplate, "
        "signature/execution blocks) and any already-compliant governing-law, term, or "
        "survival language must be LEFT UNTOUCHED. Breaking or needlessly rewriting "
        "legitimate language is a defect on your part, exactly as much as missing a real "
        "defect is. Apply the prescribed remedy (STRIKE vs cap-and-replace vs align) "
        "precisely as the rules direct."
    )

    return "\n".join(lines)


_NUMBER_WORDS = {
    1: "one (1) year",
    2: "two (2) years",
    3: "three (3) years",
    4: "four (4) years",
    5: "five (5) years",
}


def _cap_phrase(term_requirement: str, max_term_years: int) -> str:
    """Render the survival cap as a words-and-digits phrase, derived from the cap value.

    Prefers the faithful normalized requirement wording when it already names the cap in
    words; otherwise falls back to a digits rendering so any max_term_years renders."""
    phrase = _NUMBER_WORDS.get(max_term_years)
    if phrase:
        return phrase
    suffix = "year" if max_term_years == 1 else "years"
    return f"{max_term_years} ({max_term_years}) {suffix}"


def _carve_out_label(carve_outs: object) -> str:
    """Group the raw carve-out terms into the readable category list for RULE 3."""
    if not isinstance(carve_outs, Sequence):
        return ""
    terms = {_text(item).lower() for item in carve_outs if _text(item)}
    categories: list[str] = []
    if terms & {"trade secret", "trade secrets"}:
        categories.append("trade secrets")
    if terms & {
        "legal obligation",
        "legal obligations",
        "required by law",
        "applicable law",
    }:
        categories.append("legal/regulatory obligations")
    if terms & {"personal data", "data protection", "data-protection"}:
        categories.append("personal data / data-protection obligations")
    return _join_with_or(categories)
