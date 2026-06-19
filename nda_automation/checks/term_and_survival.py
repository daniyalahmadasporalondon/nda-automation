from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

from ..concept_classifier import ORDINARY_CONFIDENTIALITY_CONCEPTS
from .common import (
    ClauseResult,
    Paragraph,
    YEAR_TERM_PATTERN,
    _check,
    _clause_term_patterns,
    _clause_terms,
    _literal_word_pattern,
    _match,
    _max_term_years,
    _normalize,
    _not_present,
    _paragraph_matches,
    _term_context_patterns,
    _year_count_label,
    _year_word_value,
)
from .context import attach_structure_context
from ..review_state import _semantic_review_code, _issue_type, _generic_reason_code, CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW

CARVE_OUT_SURVIVAL_PATTERN = (
    r"\b(?:surviv(?:e|es|ed|ing|al)|remain(?:s|ed|ing)?|continu(?:e|es|ed|ing)|"
    r"last(?:s|ed|ing)?|in\s+effect|binding|required?|requires?)\b"
)
ORDINARY_SURVIVAL_SUBJECT_PATTERN = (
    r"\b(?:(?:all|the|such|ordinary|confidentiality|confidential|parties'?|party)\s+)"
    r"(?:confidentiality\s+)?(?:obligations?|undertakings?|provisions?|duties?)\b"
    r"|\bconfidentiality\s+(?:surviv(?:e|es)|continu(?:e|es)|remain(?:s)?)\b"
)
SURVIVAL_REFERENCE_SCOPE_PATTERN = (
    r"\b(?:obligations?|undertakings?|provisions?|duties?|rights?\s+and\s+obligations?)\b"
    r".{0,180}\b(?:clause|clauses|article|articles|section|sections)\b"
    r"|\b(?:clause|clauses|article|articles|section|sections)\b"
    r".{0,180}\b(?:obligations?|undertakings?|provisions?|duties?|rights?\s+and\s+obligations?)\b"
)
SURVIVAL_DURATION_VERB_PATTERN = (
    r"\bsurviv(?:e|es|ed|ing|al)\b"
    r"|\b(?:remain(?:s|ed|ing)?|continu(?:e|es|ed|ing)|in\s+effect)\b"
    r".{0,80}\b(?:after|following|expiry|expiration|termination)\b"
)
AGREEMENT_TERM_DURATION_PATTERN = (
    r"\b(?:this\s+)?agreement\b.{0,120}\b"
    r"(?:continues?|remain(?:s|ed|ing)?\s+in\s+effect|valid|effective|expires?|terminates?|term)\b"
    r"|\b(?:continues?|remain(?:s|ed|ing)?\s+in\s+effect|valid|effective|expires?|terminates?|term)\b"
    r".{0,120}\b(?:this\s+)?agreement\b"
    r"|\bterm\b\s*[:.-]\s*(?:for\s+)?"
)
NON_CONFIDENTIAL_DURATION_SUBJECT_PATTERN = (
    r"\b(?:audit|accounting|tax|payment|invoice|fee|warrant(?:y|ies)|indemnit(?:y|ies)|"
    r"liabilit(?:y|ies)|claim|claims|insurance|employment|records?|books?|tax\s+records?|"
    r"audit\s+records?)\b"
)
# The bare "indefinite words" -- perpetual / perpetually / indefinitely / in
# perpetuity -- are only a survival problem when they govern CONFIDENTIALITY.
# When the same word governs a non-survival object ("perpetual LICENSE", "remain
# indefinitely AVAILABLE", "perpetual RIGHT to use"), it is benign and must not
# trip the indefinite-survival flag. The object vocabulary is playbook-sourced
# (``indefinite_non_survival_objects``); this fallback only applies when the key
# is absent so behaviour degrades safely. Object words shorter/ambiguous enough to
# also be confidentiality nouns are deliberately excluded.
INDEFINITE_POLARITY_WORDS = ("perpetual", "perpetually", "indefinitely", "perpetuity")
DEFAULT_INDEFINITE_NON_SURVIVAL_OBJECTS = (
    "license",
    "licence",
    "right",
    "rights",
    "access",
    "available",
    "availability",
    "royalty",
    "royalties",
    "grant",
    "easement",
    "ownership",
    "title",
    "warranty",
    "warranties",
)
# A CONFIDENTIALITY-SURVIVAL predicate: language that says ordinary Confidential
# Information stays confidential / secret / undisclosed / in force, or that the
# confidentiality obligations survive or continue. This is the GOVERNANCE signal --
# an open-ended duration only makes a clause perpetual when it times one of THESE
# predicates. The closed-vocabulary "perpetual"/"indefinitely" substring match alone
# over-flags when the same word governs a party name ("Perpetual Holdings"), a product
# line ("Perpetual Motion"), a manner noun ("with perpetual diligence"), an inventory
# ("a perpetual inventory"), or agreement renewal ("indefinitely renew this agreement").
# Requiring the marker to locally govern a confidentiality-survival predicate is the
# structural fix that simultaneously catches novel "forever" wording (it governs
# survival) and stops flagging incidental "perpetual" (it governs something else).
CONFIDENTIALITY_SURVIVAL_PREDICATE_PATTERN = (
    # "(remain/stay/be/kept) confidential / secret / undisclosed". NOTE: "in force" /
    # "in effect" is DELIBERATELY excluded from this verb-led branch (it has no subject
    # requirement) because "this Agreement shall remain in force" is an AGREEMENT term,
    # not a confidentiality survival -- crediting it would wrongly scope in the agreement
    # duration. The CI-subject-led branch below still credits "...the Confidential
    # Information shall remain in force..." where CI is explicitly the subject.
    r"\b(?:remain(?:s|ed|ing)?|stay(?:s|ed|ing)?|kept|keep(?:s|ing)?|held|hold(?:s|ing)?|"
    r"be|been|being|continu(?:e|es|ed|ing)|treated|maintain(?:s|ed|ing)?|protected|preserv(?:e|es|ed|ing))\b"
    r"(?:\W+\w+){0,4}?\W+"
    r"(?:confidential|confidentiality|secret|secrecy|undisclosed|non[\s-]*disclosure)\b"
    # "confidential / confidentiality ... (remain/survive/continue/in force)"
    r"|\b(?:confidential\s+information|confidentiality(?:\s+(?:obligations?|undertakings?|provisions?|duties?))?)\b"
    r"(?:\W+\w+){0,5}?\W+"
    r"(?:surviv(?:e|es|ed|ing|al)|remain(?:s|ed|ing)?|continu(?:e|es|ed|ing)|in\s+force|in\s+effect|"
    r"confidential|secret|undisclosed)\b"
    # bare survival verb governing the confidentiality obligations
    r"|\bsurviv(?:e|es|ed|ing|al)\b"
    # "(never) expire / cease to be confidential" -- the negated-expiry idiom
    r"|\b(?:never\s+)?expire(?:s|d)?\b"
    r"|\bcease(?:s|d)?\s+to\s+be\s+(?:confidential|secret)\b"
    # "access to / disclosure of ... confidential information" -- CI is the thing
    # held/disclosed forever (gate-1 leak B: "indefinitely grant access to the
    # confidential information").
    r"|\b(?:access\s+to|disclosure\s+of|use\s+of|hold|retain(?:s|ed|ing)?)\b"
    r"(?:\W+\w+){0,4}?\W+confidential\s+information\b"
)
# STRUCTURAL backstop trigger: a negated-expiry / never-cease idiom over
# confidentiality is open-ended survival that no closed vocabulary enumerates --
# "shall not cease to be confidential at any time", "shall never cease to be
# confidential", "shall not (ever) expire", "does not expire". Treated as an
# indefinite hit (subject to the same carve-out / benign / governance filters as the
# vocabulary). The "not ... at any time" / "never" / "no longer ... never" negation is
# required so a POSITIVE bounded statement ("shall cease to be confidential after five
# years", "expires after the term") is NOT swept in.
STRUCTURAL_OPEN_ENDED_SURVIVAL_PATTERN = (
    r"\b(?:shall\s+|will\s+|does\s+|do\s+)?"
    r"(?:not\s+(?:ever\s+)?|never\s+)"
    r"(?:cease\s+to\s+be\s+(?:confidential|secret)|expire(?:s)?)"
    r"(?:\W+\w+){0,3}?(?:\bat\s+any\s+time\b)?"
    r"|\b(?:cease\s+to\s+be\s+(?:confidential|secret)|expire(?:s)?)\b"
    r"(?:\W+\w+){0,3}?\bat\s+no\s+time\b"
)
# Single source of truth: the playbook's term_and_survival
# ``longer_survival_carve_out_terms``. This in-code copy is ONLY a defensive
# fallback for a degraded/legacy clause config that arrives without the field;
# the normal review path carries the playbook list on the clause and never
# touches this tuple. It MUST stay byte-identical to the shipped
# playbook.json list (including the data-protection terms) -- a missing term
# here would WRONGLY FAIL a legitimate longer-survival carve-out (e.g. personal
# data retained as long as data-protection law requires). The drift is pinned by
# tests/test_term_carveout_drift_guard.py, which asserts this tuple equals the
# playbook field exactly; do not edit one without the other.
DEFAULT_LONGER_SURVIVAL_CARVE_OUT_TERMS = (
    "trade secret",
    "trade secrets",
    "legal obligation",
    "legal obligations",
    "required by law",
    "applicable law",
    "personal data",
    "data protection",
    "data-protection",
)
# Single source of truth: the playbook's term_and_survival ``indefinite_terms``.
# This in-code copy is ONLY a defensive fallback for a degraded/legacy clause
# config that arrives without the field; the normal review path carries the
# playbook list on the clause and never touches this tuple. It MUST stay
# byte-identical to the shipped playbook.json list -- a missing perpetual phrasing
# here would let an everlasting ordinary-CI rider slip through as "missing" rather
# than be flagged as too-long. The drift is pinned by
# tests/test_term_carveout_drift_guard.py, which asserts this tuple equals the
# playbook field exactly; do not edit one without the other.
DEFAULT_INDEFINITE_TERMS = (
    "indefinitely",
    "perpetuity",
    "perpetual",
    "perpetually",
    "perpetual confidentiality",
    "for so long as",
    "for as long as",
    "for so long as the information remains confidential",
    "ceases to have commercial value",
    "until it ceases to have commercial value",
    "until it ceases to have value",
    "for so long as it retains commercial value",
    "until released in writing",
    "until the disclosing party releases",
    "as long as it remains secret",
    "forever",
    "everlasting",
    "no expiration",
    "no expiration date",
    "unlimited period",
    "for an unlimited period",
    "without limitation of time",
    "without limitation in time",
    "until the end of time",
    "never expire",
    "permanently",
    "for all time",
    "no end date",
    "without any time limit",
    "indefinite duration",
    "without limit of time",
    "no time limitation",
    "on an enduring basis",
    "infinite period",
    "unlimited time",
    "everlastingly",
    "ad infinitum",
    "in perpetuum",
    "until the end of days",
)
GENERIC_NON_SURVIVAL_DURATION_TERMS = {
    "after termination",
    "continue",
    "continues",
    "duration",
    "effective date",
    "expiration",
    "in effect",
    "period",
    "term",
    "years",
}


def _select_term_paragraphs(
    paragraphs: List[Paragraph], term_context_patterns: List[str]
) -> List[Paragraph]:
    """Term/survival paragraphs = the search-term anchors PLUS any paragraph that states
    a confidentiality-survival predicate.

    ``_term_context_patterns`` deliberately drops the bare "year"/"years" anchor (to
    avoid sweeping in every incidental year), so a BARE confidentiality-duration clause
    that uses no "term/survive/period" keyword -- "The Confidential Information shall
    remain confidential for twenty (20) years." -- matched NO anchor and was reported
    as "missing" rather than scoped in and flagged over-cap (S7). Adding the
    confidentiality-survival predicate as an anchor scopes such a clause in. The
    predicate requires CI/confidentiality + a stays-confidential verb, so it does not
    broaden selection to unrelated year mentions.
    """
    return _paragraph_matches(
        paragraphs,
        list(term_context_patterns) + [CONFIDENTIALITY_SURVIVAL_PREDICATE_PATTERN],
    )


def _check_term_and_survival(
    _text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    context_concepts = ["term_or_survival", "trade_secret_or_legal_carveout"]
    max_years = _max_term_years(clause)
    cap_label = _year_count_label(max_years)
    term_context_patterns = _term_context_patterns(clause)
    indefinite_patterns = _indefinite_term_patterns(clause)
    term_paragraphs = _select_term_paragraphs(paragraphs, term_context_patterns)
    term_normalized = _normalize(" ".join(str(paragraph["text"]) for paragraph in term_paragraphs))
    reference_analysis = _survival_reference_analysis(term_paragraphs, paragraphs, review_context or {})
    year_terms = _extract_year_terms_with_context(term_normalized)
    scoped_year_terms = [
        term
        for term in year_terms
        if _year_term_has_required_scope(term_normalized, term, clause)
    ]
    has_term_within_cap = any(0 < term["years"] <= max_years for term in scoped_year_terms)
    # The detected ordinary term in years, as a clean scalar (the largest scoped
    # year-term found, or None). This is best-effort provenance for the corpus
    # ``term_years`` facet -- it never affects the verdict, only surfaces what the
    # check already computed so a "5-year NDA" search can match deterministically.
    detected_term_years = _detected_term_years(scoped_year_terms)
    ordinary_over_cap_terms = [
        term
        for term in scoped_year_terms
        if term["years"] > max_years and not _is_allowed_carve_out_year(term_normalized, term, clause)
    ]
    has_term_over_cap = bool(ordinary_over_cap_terms)
    ordinary_indefinite_matches = [
        match
        for pattern in indefinite_patterns
        for match in re.finditer(pattern, term_normalized)
        if not _is_allowed_carve_out_fragment(term_normalized, match.start(), match.end(), clause)
        and not _is_benign_indefinite_match(term_normalized, match, clause)
        and _indefinite_match_governs_ci_survival(term_normalized, match, clause)
    ]

    if has_term_over_cap:
        result = _check(
            clause,
            f"A term or survival period exceeds the cap of {cap_label}.",
            _paragraphs_with_scoped_year_terms(
                term_paragraphs,
                clause,
                max_years=max_years,
                over_cap=True,
                exclude_allowed_carve_out=True,
            ),
            what_to_fix=(
                "Reduce the ordinary confidentiality term or survival period "
                f"to a fixed period of {cap_label} or less."
            ),
        )
        if detected_term_years is not None:
            result["term_years"] = detected_term_years
        _attach_survival_analysis(result, reference_analysis)
        return attach_structure_context(result, review_context, context_concepts)
    if ordinary_indefinite_matches:
        result = _check(
            clause,
            f"Survival language appears indefinite or perpetual rather than capped at {cap_label}.",
            _paragraph_matches(term_paragraphs, indefinite_patterns),
            what_to_fix=(
                "Replace indefinite or perpetual ordinary confidentiality language "
                f"with a fixed period of {cap_label} or less."
            ),
        )
        if detected_term_years is not None:
            result["term_years"] = detected_term_years
        _attach_survival_analysis(result, reference_analysis)
        return attach_structure_context(result, review_context, context_concepts)
    if has_term_within_cap:
        evidence_paragraphs = _term_evidence_paragraphs(
            term_paragraphs,
            paragraphs,
            reference_analysis,
            clause,
            max_years,
        )
        reference_review_reason = _survival_reference_review_reason(reference_analysis, cap_label)
        if reference_review_reason:
            result = _match(clause, reference_review_reason, evidence_paragraphs)
            result["decision"] = "review"
            result["needs_review"] = True
            result["review_reason"] = reference_review_reason
            result["decision_reason"] = reference_review_reason
            result["what_to_fix"] = (
                "Confirm whether the referenced survival provisions include ordinary confidentiality "
                f"obligations capped at {cap_label} or less."
            )
        elif reference_analysis["confidentiality_reference_count"]:
            reason = (
                "Referenced confidentiality provisions survive within "
                f"the cap of {cap_label}."
            )
            result = _match(clause, reason, evidence_paragraphs)
        else:
            reason = f"Term or survival period is within the cap of {cap_label}."
            result = _match(clause, reason, evidence_paragraphs)
        if detected_term_years is not None:
            result["term_years"] = detected_term_years
        _attach_survival_analysis(result, reference_analysis)
        return attach_structure_context(result, review_context, context_concepts)
    result = _not_present(
        clause,
        f"No fixed term or survival period of up to {cap_label} was found.",
        term_paragraphs,
        what_to_fix=f"Add a fixed term or ordinary confidentiality survival period of {cap_label} or less.",
    )
    _attach_survival_analysis(result, reference_analysis)
    return attach_structure_context(result, review_context, context_concepts)


def _extract_year_terms_with_context(normalized: str) -> List[Dict[str, int]]:
    terms: List[Dict[str, int]] = []
    for match in re.finditer(YEAR_TERM_PATTERN, normalized):
        word_value, digit_value, parenthetical_digit, parenthetical_word, unit = match.groups()
        # The lead value is whichever of the two lead alternatives matched (a digit
        # like "5" or a spelled word like "ten"); the parenthetical value is the
        # optional "(...)" confirmation that drafting convention restates, e.g.
        # "five (5)". When BOTH a lead and a parenthetical are present and they
        # DISAGREE ("ten (5)"), trusting the parenthetical digit alone laundered a
        # 10-year term down to a compliant 5. Take the MAX of every value the match
        # surfaced so a disagreement resolves to the longer (and therefore more
        # conservative, cap-flagging) period; the AGREEMENT case ("five (5)", "5 (5)")
        # is unchanged because max(5, 5) == 5.
        candidate_values: List[int] = []
        if digit_value:
            candidate_values.append(int(digit_value))
        if word_value:
            candidate_values.append(_year_word_value(word_value))
        if parenthetical_digit:
            candidate_values.append(int(parenthetical_digit))
        if parenthetical_word:
            candidate_values.append(_year_word_value(parenthetical_word))
        if not candidate_values:
            continue
        value = max(candidate_values)
        years = value / 12 if unit.startswith("month") else value
        terms.append({"years": years, "start": match.start(), "end": match.end()})
    return terms


def _survival_reference_analysis(
    term_paragraphs: List[Paragraph],
    all_paragraphs: List[Paragraph],
    review_context: Dict[str, object],
) -> Dict[str, object]:
    paragraph_ids = {str(paragraph.get("id")) for paragraph in term_paragraphs if paragraph.get("id")}
    paragraph_lookup = {str(paragraph.get("id")): paragraph for paragraph in all_paragraphs if paragraph.get("id")}
    classifier = review_context.get("concept_classifier")
    concepts_by_section_id = {}
    if isinstance(classifier, dict) and isinstance(classifier.get("concepts_by_section_id"), dict):
        concepts_by_section_id = classifier["concepts_by_section_id"]
    reference_resolver = review_context.get("reference_resolver")
    references = reference_resolver.get("references", []) if isinstance(reference_resolver, dict) else []

    records: List[Dict[str, object]] = []
    confidentiality_reference_count = 0
    target_paragraph_ids: List[str] = []
    for reference in references:
        if not isinstance(reference, dict) or str(reference.get("paragraph_id")) not in paragraph_ids:
            continue
        paragraph_id = str(reference.get("paragraph_id") or "")
        paragraph_text = str(paragraph_lookup.get(paragraph_id, {}).get("text") or "")
        if not _is_survival_scope_reference(paragraph_text):
            continue
        target_records = []
        target_has_confidentiality = False
        for target in reference.get("targets", []):
            if not isinstance(target, dict):
                continue
            section_id = str(target.get("id") or "")
            raw_concepts = concepts_by_section_id.get(section_id, [])
            if not isinstance(raw_concepts, list):
                raw_concepts = []
            concepts = [
                str(concept)
                for concept in raw_concepts
                if str(concept)
            ]
            is_confidentiality = bool(ORDINARY_CONFIDENTIALITY_CONCEPTS.intersection(concepts))
            target_has_confidentiality = target_has_confidentiality or is_confidentiality
            for target_paragraph_id in target.get("paragraph_ids", []):
                paragraph_key = str(target_paragraph_id)
                if paragraph_key in paragraph_lookup and paragraph_key not in target_paragraph_ids:
                    target_paragraph_ids.append(paragraph_key)
            target_records.append({
                "section_id": section_id,
                "label": str(target.get("label") or ""),
                "concepts": concepts,
                "ordinary_confidentiality": is_confidentiality,
            })
        if target_has_confidentiality:
            confidentiality_reference_count += 1
        records.append({
            "reference_text": str(reference.get("reference_text") or ""),
            "paragraph_id": paragraph_id,
            "status": str(reference.get("status") or ""),
            "unresolved_numbers": [
                str(number)
                for number in reference.get("unresolved_numbers", [])
                if str(number)
            ],
            "targets": target_records,
            "ordinary_confidentiality": target_has_confidentiality,
        })

    return {
        "references": records,
        "reference_count": len(records),
        "confidentiality_reference_count": confidentiality_reference_count,
        "target_paragraph_ids": target_paragraph_ids,
    }


def _is_survival_scope_reference(paragraph_text: str) -> bool:
    normalized = _normalize(paragraph_text)
    return bool(
        re.search(CARVE_OUT_SURVIVAL_PATTERN, normalized)
        and re.search(SURVIVAL_REFERENCE_SCOPE_PATTERN, normalized)
    )


def _year_term_has_required_scope(normalized: str, term: Dict[str, int], clause: Dict[str, object]) -> bool:
    fragment = _term_sentence_fragment(normalized, term["start"], term["end"])
    if _is_non_confidential_duration_fragment(fragment):
        return False
    if _is_survival_duration_fragment(fragment):
        return bool(
            re.search(ORDINARY_SURVIVAL_SUBJECT_PATTERN, fragment)
            or _is_survival_scope_reference(fragment)
        )
    # A bare confidentiality-survival predicate ("The Confidential Information shall
    # remain confidential for twenty (20) years.", "...kept confidential for five (5)
    # years...") is an ordinary-CI survival duration even without the "survive after
    # termination" idiom. Scoping it in lets an over-cap bare period be FLAGGED rather
    # than reported as "missing", and lets a within-cap bare period be recognized as a
    # genuine match instead of falling through. Precision: the non-confidential-subject
    # guard above already excluded audit/tax/payment durations, and the confidentiality
    # predicate itself requires CI/confidentiality + a stays-confidential verb, so an
    # agreement-term or unrelated period is not swept in here.
    if re.search(CONFIDENTIALITY_SURVIVAL_PREDICATE_PATTERN, fragment):
        return True
    return bool(
        re.search(AGREEMENT_TERM_DURATION_PATTERN, fragment)
        or _has_configured_non_survival_duration_scope(fragment, clause)
    )


def _is_survival_duration_fragment(fragment: str) -> bool:
    return bool(re.search(SURVIVAL_DURATION_VERB_PATTERN, fragment))


def _is_non_confidential_duration_fragment(fragment: str) -> bool:
    if not re.search(NON_CONFIDENTIAL_DURATION_SUBJECT_PATTERN, fragment):
        return False
    return not bool(
        re.search(ORDINARY_SURVIVAL_SUBJECT_PATTERN, fragment)
        or re.search(r"\b(?:this\s+)?agreement\b", fragment)
        or _is_survival_scope_reference(fragment)
    )


def _has_configured_non_survival_duration_scope(fragment: str, clause: Dict[str, object]) -> bool:
    for term in _clause_terms(clause, "search_terms"):
        if term in GENERIC_NON_SURVIVAL_DURATION_TERMS or term.startswith("surviv"):
            continue
        if re.search(_literal_word_pattern(term), fragment, flags=re.IGNORECASE):
            return True
    return False


def _term_sentence_fragment(normalized: str, start: int, end: int) -> str:
    left = max(normalized.rfind(separator, 0, start) for separator in (".", ";"))
    right_candidates = [
        position
        for position in (normalized.find(separator, end) for separator in (".", ";"))
        if position != -1
    ]
    right = min(right_candidates) if right_candidates else len(normalized)
    return normalized[left + 1:right].strip()


def _survival_reference_review_reason(reference_analysis: Dict[str, object], cap_label: str) -> str:
    references = [
        reference
        for reference in reference_analysis.get("references", [])
        if isinstance(reference, dict)
    ]
    if not references:
        return ""

    unresolved_labels = _unresolved_reference_labels(references)
    if unresolved_labels:
        return (
            "Survival language uses cross-references that could not be fully resolved "
            f"({', '.join(unresolved_labels)}), so the ordinary confidentiality survival scope needs review."
        )

    if not reference_analysis.get("confidentiality_reference_count"):
        return (
            "Survival language is capped within "
            f"{cap_label}, but the referenced sections do not clearly classify as ordinary confidentiality obligations."
        )

    return ""


def _unresolved_reference_labels(references: List[Dict[str, object]]) -> List[str]:
    labels: List[str] = []
    for reference in references:
        if str(reference.get("status") or "") not in {"partial", "unresolved"}:
            continue
        reference_text = str(reference.get("reference_text") or "").strip()
        unresolved_numbers = [
            str(number)
            for number in reference.get("unresolved_numbers", [])
            if str(number)
        ]
        if reference_text and unresolved_numbers:
            labels.append(f"{reference_text}: {', '.join(unresolved_numbers)}")
        elif reference_text:
            labels.append(reference_text)
        elif unresolved_numbers:
            labels.extend(unresolved_numbers)
    return labels


def _term_evidence_paragraphs(
    term_paragraphs: List[Paragraph],
    all_paragraphs: List[Paragraph],
    reference_analysis: Dict[str, object],
    clause: Dict[str, object],
    max_years: int,
) -> List[Paragraph]:
    evidence = _paragraphs_with_scoped_year_terms(
        term_paragraphs,
        clause,
        max_years=max_years,
        within_cap=True,
    )
    paragraph_lookup = {str(paragraph.get("id")): paragraph for paragraph in all_paragraphs if paragraph.get("id")}
    for paragraph_id in reference_analysis.get("target_paragraph_ids", []):
        paragraph = paragraph_lookup.get(str(paragraph_id))
        if paragraph:
            evidence.append(paragraph)
    return evidence


def _paragraphs_with_scoped_year_terms(
    paragraphs: List[Paragraph],
    clause: Dict[str, object],
    *,
    max_years: int,
    within_cap: bool = False,
    over_cap: bool = False,
    exclude_allowed_carve_out: bool = False,
) -> List[Paragraph]:
    matches: List[Paragraph] = []
    for paragraph in paragraphs:
        normalized = _normalize(str(paragraph.get("text") or ""))
        terms = [
            term
            for term in _extract_year_terms_with_context(normalized)
            if _year_term_has_required_scope(normalized, term, clause)
        ]
        if exclude_allowed_carve_out:
            terms = [
                term
                for term in terms
                if not _is_allowed_carve_out_year(normalized, term, clause)
            ]
        if within_cap:
            terms = [term for term in terms if 0 < term["years"] <= max_years]
        if over_cap:
            terms = [term for term in terms if term["years"] > max_years]
        if terms:
            matches.append(paragraph)
    return matches


def _detected_term_years(scoped_year_terms: List[Dict[str, object]]) -> float | None:
    """The largest scoped ordinary year-term as a clean scalar (or None).

    Pure provenance for the corpus ``term_years`` facet: it reads what the check
    already extracted and never influences the verdict. None when no scoped term
    was found, so a term search degrades to "unknown" rather than a wrong match.
    """
    years = [
        float(term["years"])
        for term in scoped_year_terms
        if isinstance(term, dict) and isinstance(term.get("years"), (int, float)) and float(term["years"]) > 0
    ]
    if not years:
        return None
    return max(years)


def _attach_survival_analysis(result: ClauseResult, reference_analysis: Dict[str, object]) -> None:
    if not reference_analysis.get("reference_count"):
        return
    result["term_survival_analysis"] = {
        "reference_count": reference_analysis["reference_count"],
        "confidentiality_reference_count": reference_analysis["confidentiality_reference_count"],
        "references": reference_analysis["references"],
    }


def _indefinite_non_survival_objects(clause: Dict[str, object]) -> List[str]:
    configured = clause.get("indefinite_non_survival_objects")
    if isinstance(configured, list):
        objects = [str(term).lower().strip() for term in configured if str(term).strip()]
    else:
        objects = list(DEFAULT_INDEFINITE_NON_SURVIVAL_OBJECTS)
    return objects


# Tokens that, when they IMMEDIATELY follow an attributive ``perpetual``/``perpetually``
# (i.e. ``perpetual <token>``), mean the marker times the confidentiality survival
# rather than modifying a benign noun: "perpetual confidentiality", "perpetually
# confidential". Anything else after attributive ``perpetual`` ("perpetual diligence /
# inventory / holdings / motion / license") is the marker modifying that noun.
_CONFIDENTIALITY_DURATION_HEAD_PATTERN = re.compile(
    r"^(?:\W+(?:the|its|such|all|any|of|to|in)\b)*"
    r"\W*(?:confidential(?:ity)?|secret|secrecy|undisclosed|non[\s-]*disclosure)\b"
)


def _indefinite_match_governs_ci_survival(
    normalized: str, match: "re.Match[str]", clause: Dict[str, object]
) -> bool:
    """Governance gate: keep an indefinite-vocab hit only when an open-ended duration
    GOVERNS THE CONFIDENTIALITY SURVIVAL.

    The POLARITY words (perpetual / perpetually / indefinitely / in perpetuity) are
    ambiguous -- the same substring appears incidentally in a party name ("Perpetual
    Holdings"), a product line, a manner noun ("perpetual diligence"), an inventory, or
    agreement renewal -- so they fire ONLY when they locally time a confidentiality
    survival (``_indefinite_polarity_governs_ci_survival``). The rest of the vocabulary
    (forever, everlasting, never expire, no end date, for an indefinite duration, ...)
    is unambiguous open-ended-survival wording with no benign reading, so it always
    fires. Fail-safe: any unexpected input keeps the flag (treat as governing).
    """
    try:
        token = normalized[match.start():match.end()].strip().lower()
        if not any(word in token for word in INDEFINITE_POLARITY_WORDS):
            return True
        fragment, relative_start, relative_end = _term_fragment_bounds(
            normalized, match.start(), match.end()
        )
        return _indefinite_polarity_governs_ci_survival(
            fragment, relative_start, relative_end, clause
        )
    except Exception:
        return True


def _indefinite_polarity_governs_ci_survival(
    fragment: str, relative_start: int, relative_end: int, clause: Dict[str, object]
) -> bool:
    """STRUCTURAL governance gate for the POLARITY markers (perpetual / perpetually /
    indefinitely / in perpetuity).

    Returns True only when the marker actually TIMES an ordinary-CI confidentiality
    survival -- i.e. an open-ended duration GOVERNS THE CONFIDENTIALITY SURVIVAL. This
    is what turns the closed-vocabulary substring match into a governance check:

    * FIRE -- the marker is the duration of a confidentiality-survival predicate:
        "shall remain confidential in perpetuity", "shall never expire",
        "indefinitely grant access to the confidential information" (CI held forever),
        "remain perpetually available" (ordinary CI made perpetually available).
    * DEMOTE -- the marker governs a NON-confidentiality noun, with no
      confidentiality-survival predicate it locally times:
        "between Aspora and Perpetual Holdings Ltd" (party name),
        "for the Perpetual Motion product line", "with perpetual diligence",
        "maintain a perpetual inventory", "indefinitely renew this agreement".

    The discriminator is LOCAL: "...kept confidential for five (5) years with perpetual
    diligence" has a confidentiality-survival predicate, but it is already CAPPED and
    the marker attaches to "diligence", so the marker does not govern an OPEN-ENDED CI
    survival -- demote. A capped predicate is detected by a numeric YEAR_TERM between
    the predicate and the marker / in the marker's local window.

    Only the POLARITY words are gated here. The unambiguous open-ended vocabulary
    (forever, everlasting, never expire, no end date, for an indefinite duration, ...)
    is not a polarity word and always fires -- it has no benign reading.
    """
    token = fragment[relative_start:relative_end].strip().lower()

    # ``perpetual <confidentiality head>`` ("perpetual confidentiality", "perpetually
    # confidential") is unambiguously a perpetual confidentiality duration -> fire.
    if token in {"perpetual", "perpetually"} and _CONFIDENTIALITY_DURATION_HEAD_PATTERN.match(
        fragment[relative_end:]
    ):
        return True

    # Otherwise -- every polarity marker (attributive ``perpetual <benign noun>``,
    # adverbial ``perpetually`` / ``indefinitely`` / ``in perpetuity``) -- fires only
    # when ordinary CONFIDENTIALITY sits UNCAPPED in the marker's local window (before
    # or after).
    #
    # FAIL-SAFE direction (the forever-rework lesson: a spared false-positive is cheap, a
    # LEAKED ordinary-CI perpetual rider is not): the marker fires whenever a
    # confidentiality term it could govern sits in its window uncapped --
    # "...remain confidential in perpetuity", "...the duty continues indefinitely with
    # respect to all Confidential Information", "...the Confidential Information shall be
    # held in perpetuity", "...survive perpetually as to the Confidential Information",
    # "...the confidential information shall remain perpetually available". The benign
    # cases carry NO confidentiality term in the marker's window ("Perpetual Holdings
    # Ltd", "Perpetual Motion product line", "a perpetual inventory", "indefinitely renew
    # this agreement") or carry one that is already CAPPED in a different/local segment
    # ("kept confidential for five (5) years with perpetual diligence"), so they demote.
    return _confidentiality_in_marker_window(
        fragment, relative_start, relative_end, before_only=False
    )


# A GOVERNED-SURVIVAL signal whose presence in an adverbial marker's window means the
# marker could be timing the confidentiality survival:
#   * a confidentiality term ("confidential", "confidentiality", "secret", ...), or
#   * a survival verb ("survive(s/d)", "surviving", "survival") -- "the rights and
#     obligations ... will SURVIVE ... for perpetuity", whose survival subject carries
#     ordinary confidentiality.
# (A carve-out term carried alongside -- "trade secrets" -- does NOT neutralise this: an
# ordinary-CI subject conjoined behind a carve-out signal is still an ordinary-CI
# perpetual rider, which the upstream carve-out scoping handles separately.)
GOVERNED_SURVIVAL_SIGNAL_PATTERN = (
    r"\b(?:confidential(?:ity)?|secret|secrecy|undisclosed|non[\s-]*disclosure"
    r"|surviv(?:e|es|ed|ing|al))\b"
)


def _confidentiality_in_marker_window(
    fragment: str, relative_start: int, relative_end: int, *, before_only: bool
) -> bool:
    """Whether an ordinary-survival signal sits UNCAPPED in the adverbial marker's window.

    The window is the marker's own clause-segment (bounded by commas/semicolons), so a
    confidentiality term capped in a DIFFERENT segment cannot credit the marker. A
    numeric YEAR_TERM caps it: if a year-term sits between the confidentiality term and
    the marker the survival is already fixed-length and the marker is timing something
    else ("kept confidential for five (5) years with perpetual diligence") -> demote.

    Fires for a confidentiality term either side of the marker (the marker may follow
    the subject -- "the Confidential Information shall be held in perpetuity" -- or lead
    it -- "indefinitely grant access to the confidential information", "survive
    perpetually, including the Confidential Information").

    The window is the whole SENTENCE-bounded fragment (``_term_fragment_bounds`` already
    walled it off at ``.``/``;`` from unrelated sentences). Crossing commas is
    deliberate and fail-safe: a confidentiality term enumerated after a comma --
    "...survive perpetually, including the Confidential Information" -- is still governed
    by the marker. The CAP guard (a numeric YEAR_TERM between the term and the marker)
    is what prevents an already-capped confidentiality term in the same sentence from
    crediting the marker ("kept confidential for five (5) years with perpetual
    diligence" / "the confidentiality obligations survive for five (5) years ...
    perpetual holdings ltd").
    """
    before = fragment[:relative_start]
    after = "" if before_only else fragment[relative_end:]

    before_matches = list(re.finditer(GOVERNED_SURVIVAL_SIGNAL_PATTERN, before))
    if before_matches:
        last = before_matches[-1]
        if not re.search(YEAR_TERM_PATTERN, before[last.end():]):
            return True

    if after:
        first = re.search(GOVERNED_SURVIVAL_SIGNAL_PATTERN, after)
        if first and not re.search(YEAR_TERM_PATTERN, after[:first.start()]):
            return True
    return False


def _confidentiality_survival_predicate_in_window(
    fragment: str, relative_start: int, relative_end: int, *, before_only: bool
) -> bool:
    """Whether an UNCAPPED confidentiality-survival predicate locally governs the marker.

    Used for (a) the attributive ``perpetual``/``perpetually`` fall-through (a forward
    benign noun has already been ruled out, so only a PRECEDING survival predicate can
    credit it -- "the confidential information shall remain perpetually available") and
    (b) the benign-object demotion guard in ``_is_benign_indefinite_match``. The window
    is the marker's own clause-segment; a numeric YEAR_TERM between the predicate and
    the marker caps the survival and withholds credit.
    """
    before = _marker_clause_segment_before(fragment, relative_start)
    after = "" if before_only else _marker_clause_segment_after(fragment, relative_end)

    # A confidentiality-survival predicate immediately BEFORE the marker, with no
    # numeric cap between it and the marker.
    before_matches = list(re.finditer(CONFIDENTIALITY_SURVIVAL_PREDICATE_PATTERN, before))
    if before_matches:
        last = before_matches[-1]
        tail = before[last.end():]
        if not re.search(YEAR_TERM_PATTERN, tail):
            return True

    # A confidentiality-survival predicate AFTER the marker (the marker leads, e.g.
    # "indefinitely grant access to the confidential information"), with no numeric cap
    # between the marker and the predicate.
    if after:
        first = re.search(CONFIDENTIALITY_SURVIVAL_PREDICATE_PATTERN, after)
        if first:
            head = after[:first.start()]
            if not re.search(YEAR_TERM_PATTERN, head):
                return True
    return False


def _marker_clause_segment_before(fragment: str, relative_start: int) -> str:
    """Text from the nearest preceding comma/semicolon up to the marker."""
    left = max(
        fragment.rfind(separator, 0, relative_start) for separator in (",", ";")
    )
    return fragment[left + 1:relative_start]


def _marker_clause_segment_after(fragment: str, relative_end: int) -> str:
    """Text from the marker up to the next comma/semicolon."""
    candidates = [
        position
        for position in (fragment.find(separator, relative_end) for separator in (",", ";"))
        if position != -1
    ]
    right = min(candidates) if candidates else len(fragment)
    return fragment[relative_end:right]


def _is_benign_indefinite_match(
    normalized: str, match: "re.Match[str]", clause: Dict[str, object]
) -> bool:
    """Demote an indefinite-survival hit that does not actually make CONFIDENTIALITY
    indefinite.

    Two narrowly-scoped, principled demotions (both precision-preserving against the
    genuine perpetual riders, which are about confidentiality and uncapped):

    * POLARITY (CO-15 class): an "indefinite word" (perpetual / perpetually /
      indefinitely / in perpetuity) that governs a NON-survival object -- "a
      perpetual *license* to use the platform", "remain indefinitely *available*" --
      is benign. The object vocabulary is playbook-sourced. A genuine
      "...confidential in perpetuity" / "continue indefinitely" hit is NOT followed
      by such an object, so it still fires.

    * CAPPED DURATION CONNECTOR (CO-6 class): a bare "for so/as long as" is only a
      duration *connector*, not inherently perpetual. When its own sentence also
      states an explicit numeric cap ("...for as long as it is employed ... and for
      two (2) years following termination"), the survival is capped and the hit is
      benign. The genuine uncapped rider ("...and for so long as it retains
      commercial value") has no numeric period in its sentence and still fires.

    DELIBERATELY OMITTED -- a "carve-out-led sentence" demotion (sparing a legitimate
    trade-secret-ONLY perpetual rider, e.g. "with respect to trade secrets, the
    obligations of confidentiality shall survive perpetually") is NOT implemented
    here. Every attempt leaked: a leading scoping signal can be paired with an
    ordinary-CI subject held in perpetuity anywhere in the sentence (before OR after
    the trigger, via synonyms / word-order), turning a benign mild false-positive
    into a SERIOUS false-negative -- a real abusive ordinary-CI perpetual rider
    passing clean. Per the asymmetry (a spared FP is cheap; a leaked FN is not), such
    a rider is intentionally left to flag for review rather than risk laundering an
    ordinary-CI perpetuity. The separate, sentence-local carve-out scoping in
    ``_is_allowed_carve_out_fragment`` still passes the well-formed carve-out cases it
    already covered; only the broad "signal-leads-the-sentence" shortcut is gone.

    Fail-safe: any unexpected input is swallowed and treated as NOT benign (keep the
    flag) so a malformed clause never silently passes review.
    """
    try:
        token = normalized[match.start():match.end()].strip().lower()
        fragment, relative_start, relative_end = _term_fragment_bounds(
            normalized, match.start(), match.end()
        )

        # --- POLARITY demotion: indefinite word -> non-survival object ---
        if any(word in token for word in INDEFINITE_POLARITY_WORDS):
            objects = _indefinite_non_survival_objects(clause)
            # Guard the same asymmetry the carve-out-present branch guards: a
            # trailing non-survival object only makes the trigger benign when
            # ordinary confidentiality is NOT the SUBJECT preceding the trigger.
            # "The confidential information shall remain perpetually available."
            # has ordinary CI as the subject -- the rider IS perpetual ordinary
            # confidentiality and must still FAIL, not be laundered by the
            # trailing object "available". A non-CI subject ("a perpetual license",
            # "personal data shall remain perpetually available") still demotes.
            # A trailing non-survival object also fails to launder the trigger when the
            # marker GOVERNS a confidentiality-survival predicate -- "indefinitely grant
            # ACCESS to the confidential information" makes CI itself perpetually
            # accessible (CI is the governed object, gate-1 leak B), so the object word
            # "access" must not demote it. The governance window check captures that
            # ("access to ... confidential information" is a survival predicate),
            # mirroring the subject-side guard for the object side.
            governs_ci = _confidentiality_survival_predicate_in_window(
                fragment, relative_start, relative_end, before_only=False
            )
            if objects and not governs_ci and not _ordinary_ci_subject_present(
                fragment[:relative_start]
            ):
                object_alt = "|".join(re.escape(obj).replace(r"\ ", r"\s+") for obj in objects)
                # Allow up to two filler words ("a perpetual license", "remain
                # indefinitely available", "perpetual right to use") between the
                # indefinite word and the governed object.
                after = fragment[relative_end:]
                if re.match(rf"(?:\W+\w+){{0,2}}\W+(?:{object_alt})\b", after):
                    return True

        # --- CAPPED-DURATION demotion: "for so/as long as" ---
        if re.fullmatch(r"for\s+(?:so|as)\s+long\s+as", token):
            # (a) An explicit numeric period AFTER the connector caps the survival,
            #     so the bare connector is not perpetual ("for as long as X, and for
            #     two (2) years"). But the numeric period must actually GOVERN the
            #     confidentiality survival, not merely sit somewhere later in the
            #     sentence: a throwaway period in a DIFFERENT comma-segment -- a cure /
            #     notice / payment period, e.g. "...for so long as it is secret, with a
            #     6 months cure period." -- caps the cure, NOT the survival, and must
            #     not launder the uncapped rider. So we bound the numeric search to the
            #     survival sub-clause: from just after the connector up to the next
            #     clause-boundary comma/semicolon, UNLESS that next segment is itself a
            #     survival continuation ("...and for two (2) years following
            #     termination") joined by "and"/"or"/"&", in which case the period
            #     genuinely extends the survival and still caps it. A number that
            #     precedes the connector caps a DIFFERENT (prior) clause and is ignored
            #     for the same reason -- we look only after the connector.
            if re.search(YEAR_TERM_PATTERN, _survival_subclause_after_connector(fragment, relative_end)):
                return True
            # (b) The connector governs the AGREEMENT TERM, not confidentiality
            #     survival ("this Agreement shall continue in effect for so long as
            #     the parties have a business relationship") -- a benign relationship-
            #     scoped agreement term, with ordinary CI capped in a separate clause.
            #     A genuine CI rider ("...obligations continue ... for so long as it
            #     retains commercial value") is governed by confidentiality, not the
            #     Agreement, so it still fires.
            before_connector = fragment[:relative_start]
            if re.search(AGREEMENT_TERM_DURATION_PATTERN, before_connector) and not re.search(
                ORDINARY_SURVIVAL_SUBJECT_PATTERN, before_connector
            ):
                return True
    except Exception:
        # Fail-safe: never let a guard crash the board poll; keep the (stricter)
        # flag if the input is unexpected.
        return False
    return False


# A comma-segment that continues the SAME survival statement (rather than starting an
# unrelated cure/notice/payment period) is joined to the prior segment by a bare
# conjunction. Only then does a numeric period in that later segment genuinely extend
# (and therefore cap) the confidentiality survival, e.g. "...for as long as the
# recipient is engaged, and for two (2) years following termination". A segment that
# opens with anything else ("..., with a 6 months cure period", "..., upon 30 days
# notice", "..., subject to a 12 month payment term") is a different obligation and its
# period must NOT be read as capping the survival.
_SURVIVAL_CONTINUATION_LEAD_PATTERN = re.compile(
    r"^\s*(?:and|or|&|plus)\b",
)


def _survival_subclause_after_connector(fragment: str, relative_end: int) -> str:
    """The survival sub-clause text following a "for so/as long as" connector.

    Bounds the numeric-cap search to the comma-segment the connector actually
    governs, extended across any further segments that are bare survival
    continuations (joined by and/or/&/plus). Stops at the first comma-segment that
    introduces a different obligation (cure/notice/payment), so a decoy duration
    there cannot launder an uncapped confidentiality rider.
    """
    after = fragment[relative_end:]
    segments = after.split(",")
    collected = [segments[0]]
    for segment in segments[1:]:
        if _SURVIVAL_CONTINUATION_LEAD_PATTERN.match(segment):
            collected.append(segment)
            continue
        break
    return ",".join(collected)


def _is_allowed_carve_out_year(normalized: str, term: Dict[str, int], clause: Dict[str, object]) -> bool:
    return _is_allowed_carve_out_fragment(normalized, term["start"], term["end"], clause)


def _is_allowed_carve_out_fragment(normalized: str, start: int, end: int, clause: Dict[str, object]) -> bool:
    fragment, relative_start, relative_end = _term_fragment_bounds(normalized, start, end)
    carve_out_patterns = _carve_out_context_patterns(clause)
    if not any(re.search(pattern, fragment) for pattern in carve_out_patterns):
        return False
    return bool(
        any(_carve_out_scoped_before_term(fragment, relative_start, pattern) for pattern in carve_out_patterns)
        or any(_term_scoped_to_carve_out(fragment, relative_end, pattern) for pattern in carve_out_patterns)
        or any(
            _carve_out_governs_term_sentence(fragment, relative_start, relative_end, pattern)
            for pattern in carve_out_patterns
        )
    )


def _carve_out_scoped_before_term(fragment: str, term_start: int, carve_out_pattern: str) -> bool:
    before_term = fragment[:term_start]
    carve_out_matches = list(re.finditer(carve_out_pattern, before_term))
    if not carve_out_matches:
        return False

    last_carve_out = carve_out_matches[-1]
    scoped_text = before_term[last_carve_out.start():]
    if not re.search(CARVE_OUT_SURVIVAL_PATTERN, scoped_text):
        return False
    # An ordinary-CI subject sitting BETWEEN the carve-out term and the trigger means
    # ordinary confidentiality co-governs the survival, so the carve-out does not get
    # credit. This uses ``_strong_ordinary_ci_subject_present`` -- broader than the
    # narrow subject+verb ``ORDINARY_SURVIVAL_SUBJECT_PATTERN`` (it must catch a LEADING
    # carve-out idiom: "As required by applicable law, all Confidential Information shall
    # remain confidential in perpetuity." puts "all confidential information ... remain
    # confidential" in this between-window -- an ordinary-CI perpetual rider wearing a
    # carve-out rationale that must still FAIL, gate-1 leak A) -- but NOT the bare-
    # "information" alternative, which a legitimate carve-out scopes as its OWN subject
    # ("trade secrets and information subject to legal or regulatory obligations remain
    # protected for as long as ... law requires"). The strong pattern requires the
    # "confidential"/"confidentiality" qualifier, so it separates the real rider from
    # the carve-out's own scoped information.
    if _strong_ordinary_ci_subject_present(before_term[last_carve_out.end():]):
        return False
    # Ordinary CI must not CO-GOVERN the survival as a conjoined subject in the
    # SAME sub-clause as the carve-out, e.g. "the confidential information and trade
    # secrets shall remain confidential in perpetuity" -- an ordinary-CI perpetual
    # rider wearing a carve-out word, which must still FAIL. (Pre-existing hole the
    # comma-bounded window left open; closed here as part of widening the scope.)
    # Only the carve-out's own sub-clause is inspected: an exception/scoping connector
    # ("except", "other than", "save for", "with respect to", ...) starts the carve-out
    # clause, so an ordinary capped term in a PRIOR clause ("...survive for five years,
    # except trade secrets ...") is correctly left untouched.
    leading_scope = _carve_out_subclause_lead(before_term, last_carve_out.start())
    if _ordinary_ci_subject_present(leading_scope):
        return False
    return True


def _term_scoped_to_carve_out(fragment: str, term_end: int, carve_out_pattern: str) -> bool:
    after_term = _fragment_after_term_until_next_duration(fragment, term_end)
    return bool(
        re.search(
            rf"\b(?:for|as\s+to|with\s+respect\s+to|in\s+respect\s+of|solely\s+for|limited\s+to)\s+(?:the\s+)?{carve_out_pattern}\b",
            after_term,
        )
    )


def _carve_out_governs_term_sentence(
    fragment: str, term_start: int, term_end: int, carve_out_pattern: str
) -> bool:
    """Allow a perpetual trigger when the carve-out term GOVERNS it sentence-locally.

    Covers the common natural phrasings the comma-bounded window used to fail:
      - "trade secrets shall be kept confidential for as long as ..." (carve-out
        is the sentence subject; the trigger itself is the survival verb)
      - "personal data shall be retained for as long as data-protection law requires"
      - "with respect to trade secrets, confidentiality shall continue in perpetuity"

    Precision is preserved two ways: (1) the carve-out term must appear in the
    SAME sentence as the trigger (the comma split is gone, but the sentence split
    survives, so an ordinary-CI perpetual rider in a different sentence is still
    rejected upstream by the no-carve-out-in-fragment guard); (2) ordinary CI must
    not co-govern the trigger -- if an ordinary-confidentiality survival subject
    sits between the carve-out term and the trigger (e.g. "trade secrets and the
    confidentiality obligations survive in perpetuity"), the carve-out does not
    get credit and the rider still fails.
    """
    before_term = fragment[:term_start]
    carve_out_matches = list(re.finditer(carve_out_pattern, before_term))
    after_signal_scope = False
    if not carve_out_matches:
        # ASYMMETRY FIX (DEFECT A / NB-W1b): an UNCONDITIONAL perpetual trigger
        # (perpetual / perpetually / indefinitely / in perpetuity) with a trailing
        # carve-out idiom is only benign when ordinary confidentiality is NOT the
        # SUBJECT governed by the trigger. Mirror the guard the carve-out-present
        # branch below already applies (anchored at the carve-out sub-clause lead,
        # so a prior CAPPED ordinary-CI clause isolated by an exception connector --
        # "...survive five years, except that information ... required by applicable
        # law" -- is left untouched). Otherwise a trailing "...to comply with
        # applicable law" rationale launders an ordinary-CI perpetual rider to PASS,
        # e.g. "The confidential information shall remain confidential indefinitely
        # to comply with applicable law." -- ordinary CI IS held forever, so it must
        # still FAIL. A non-CI subject ("personal data shall be retained for as long
        # as required by applicable law") has no ordinary-CI subject leading the
        # trigger and stays a legitimate longer-survival carve-out. The guard is
        # scoped to the perpetual WORDS: a bare "for so/as long as" duration
        # connector whose period is genuinely SET by law ("for as long as required
        # by applicable law") is bounded, not perpetual, and is handled below.
        trigger_token = fragment[term_start:term_end].strip().lower()
        if any(word in trigger_token for word in INDEFINITE_POLARITY_WORDS) and (
            _ordinary_ci_subject_present(
                _carve_out_subclause_lead(before_term, len(before_term))
            )
        ):
            return False
        # The carve-out may trail the trigger behind an explicit scoping signal,
        # e.g. "...shall continue for as long as required, for trade secrets", or
        # behind a requirement/necessity idiom that itself names the carve-out, e.g.
        # "...retained for as long as required by applicable law" / "...as long as
        # data-protection law requires". These are textbook longer-survival carve-outs.
        after_term = _fragment_after_term_until_next_duration(fragment, term_end)
        return bool(
            re.search(
                rf"\b(?:for|as\s+to|with\s+respect\s+to|in\s+respect\s+of|"
                rf"solely\s+for|limited\s+to|in\s+the\s+case\s+of|"
                rf"required\s+by|require[ds]?\s+by|as\s+required\s+by|"
                rf"mandated\s+by|necessary\s+(?:to\s+comply\s+with|for|under)|"
                rf"to\s+comply\s+with|to\s+satisfy)\s+"
                # Allow a determiner ("a"/"an"/"any"/"the") between the requirement idiom
                # and the carve-out term: "required by a legal obligation" / "required by
                # any legal obligation" are the same longer-survival carve-out as
                # "required by the applicable law" -- the article must not break the
                # requirement-idiom guard (gate-3 Family 2).
                rf"(?:(?:the|a|an|any)\s+)?(?:applicable\s+)?{carve_out_pattern}\b",
                after_term,
            )
            # "...for as long as <carve-out term> requires/mandates/permits": the
            # carve-out term leads the requirement that sets the longer period.
            or re.search(
                rf"\b{carve_out_pattern}\b\s+(?:law\s+)?"
                rf"(?:requires?|require[ds]?|mandates?|permits?|allows?|so\s+requires?)\b",
                after_term,
            )
        )

    last_carve_out = carve_out_matches[-1]
    # An ordinary-CI subject must not CO-GOVERN the trigger. If ordinary
    # confidentiality is itself (also) the thing surviving -- whether it sits
    # between the carve-out term and the trigger ("trade secrets and the
    # confidentiality obligations survive in perpetuity") or leads the carve-out as
    # a conjoined subject ("the confidential information and trade secrets ... in
    # perpetuity") -- the rider is an ordinary-CI perpetual and must still fail.
    between = before_term[last_carve_out.end():]
    if _ordinary_ci_subject_present(between):
        return False
    leading_scope = before_term[:last_carve_out.start()]
    # An explicit scoping signal ("with respect to trade secrets, ...") narrows the
    # survival to the carve-out even when ordinary CI was the subject of a prior
    # (already-comma-or-semicolon-bounded) clause; otherwise an ordinary-CI subject
    # in front of the carve-out means ordinary CI co-governs the trigger -> fail.
    if re.search(
        r"\b(?:with\s+respect\s+to|as\s+to|in\s+respect\s+of|in\s+the\s+case\s+of|"
        r"for|regarding|concerning)\s*$",
        leading_scope.rstrip(),
    ):
        after_signal_scope = True
    if not after_signal_scope and _ordinary_ci_subject_present(
        _carve_out_subclause_lead(leading_scope, len(leading_scope))
    ):
        return False
    # Carve-out leads the clause (subject position): nothing but the carve-out
    # and connective words precede it, or a scoping signal introduces it.
    return True


# Ordinary-confidentiality SUBJECT noun phrases (the thing whose survival is being
# set), used to detect ordinary-CI co-governing a perpetual trigger so a carve-out
# mention cannot launder an ordinary-CI perpetual rider. Broader than
# ORDINARY_SURVIVAL_SUBJECT_PATTERN because here we are scanning a subject slot, not
# a subject+verb collocation.
ORDINARY_CI_SUBJECT_PATTERN = (
    r"\b(?:confidentiality\s+(?:obligations?|undertakings?|provisions?|duties?)"
    r"|(?:confidential\s+)?information"
    r"|(?:the|all|such|any)\s+(?:confidential\s+)?information"
    r"|obligations?\s+of\s+confidentiality"
    r"|confidentiality\s+obligations?"
    r"|(?:ordinary\s+)?confidential(?:ity)?\s+(?:obligations?|undertakings?|provisions?|duties?))\b"
)


def _ordinary_ci_subject_present(text: str) -> bool:
    return bool(
        re.search(ORDINARY_CI_SUBJECT_PATTERN, text)
        or re.search(ORDINARY_SURVIVAL_SUBJECT_PATTERN, text)
    )


# A STRONG ordinary-CI subject -- "confidential information" / "confidentiality
# obligations" and friends -- but NOT a bare "information". Bare "information" is too
# loose for a co-governance window because a legitimate carve-out routinely scopes its
# OWN subject as "information subject to legal or regulatory obligations" / "information
# the law requires to be retained": that information is the carve-out's subject, not
# ordinary CI, so the bare-"information" alternative wrongly rejected the carve-out.
# This requires the "confidential"/"confidentiality" qualifier (or a survival
# subject+verb collocation), which "all confidential information shall remain
# confidential" (a real ordinary-CI perpetual rider) satisfies and "information subject
# to legal obligations" does not.
STRONG_ORDINARY_CI_SUBJECT_PATTERN = (
    r"\b(?:confidential\s+information"
    r"|confidentiality\s+(?:obligations?|undertakings?|provisions?|duties?)"
    r"|obligations?\s+of\s+confidentiality"
    r"|(?:ordinary\s+)?confidential(?:ity)?\s+(?:obligations?|undertakings?|provisions?|duties?))\b"
)


def _strong_ordinary_ci_subject_present(text: str) -> bool:
    return bool(
        re.search(STRONG_ORDINARY_CI_SUBJECT_PATTERN, text)
        or re.search(ORDINARY_SURVIVAL_SUBJECT_PATTERN, text)
    )


# Connectors that introduce a carve-out sub-clause; an ordinary capped term sitting
# in front of one of these is a SEPARATE clause and must not be read as co-governing
# the carve-out's (longer) survival.
CARVE_OUT_EXCEPTION_CONNECTOR_PATTERN = (
    r"\b(?:except(?:ing|\s+that|\s+for)?|other\s+than|save\s+(?:for|as|that)?|"
    r"but\s+(?:for|not)|provided\s+that|with\s+respect\s+to|as\s+to|in\s+respect\s+of|"
    r"in\s+the\s+case\s+of|regarding|concerning|for)\b"
)


def _carve_out_subclause_lead(before_term: str, carve_out_start: int) -> str:
    """Text from the carve-out's own sub-clause start up to the carve-out term.

    Anchors at the last exception/scoping connector before the carve-out so that an
    ordinary capped term in a PRIOR clause ("...survive for five years, except trade
    secrets...") is excluded, while a conjoined ordinary-CI subject in the SAME
    sub-clause ("...the confidential information and trade secrets...") is retained
    and therefore rejected by the co-governance guard.
    """
    leading = before_term[:carve_out_start]
    connectors = list(re.finditer(CARVE_OUT_EXCEPTION_CONNECTOR_PATTERN, leading))
    if connectors:
        return leading[connectors[-1].end():]
    return leading


def _fragment_after_term_until_next_duration(fragment: str, term_end: int) -> str:
    after_term = fragment[term_end:]
    next_duration = re.search(YEAR_TERM_PATTERN, after_term)
    if next_duration:
        return after_term[:next_duration.start()]
    return after_term


def _indefinite_term_patterns(clause: Dict[str, object]) -> List[str]:
    """Literal-word patterns for the indefinite/perpetual vocabulary.

    Uses the playbook's ``indefinite_terms`` on the clause (the normal review path),
    falling back to the byte-identical in-code copy (drift-guarded by
    tests/test_term_carveout_drift_guard.py) only when a degraded/legacy clause
    arrives without the field, so a perpetual rider is still detected.
    """
    configured = clause.get("indefinite_terms")
    if isinstance(configured, list):
        patterns = _clause_term_patterns(clause, "indefinite_terms")
    else:
        patterns = [
            _literal_word_pattern(term)
            for term in DEFAULT_INDEFINITE_TERMS
            if str(term).strip()
        ]
    # STRUCTURAL BACKSTOP -- a negated-expiry / never-cease idiom over confidentiality
    # is open-ended survival that no closed vocabulary enumerates: "shall not cease to
    # be confidential at any time", "shall never cease to be confidential", "shall not
    # expire". These say CI stays confidential with no end, so they are indefinite by
    # governance, not by keyword. Appended to the vocab patterns so they flow through
    # the SAME carve-out / benign / governance filters (a trade-secret-only or
    # personal-data carve-out wearing this idiom is still demoted upstream).
    patterns.append(STRUCTURAL_OPEN_ENDED_SURVIVAL_PATTERN)
    return patterns


def _carve_out_context_patterns(clause: Dict[str, object]) -> List[str]:
    configured_terms = clause.get("longer_survival_carve_out_terms")
    if isinstance(configured_terms, list):
        terms = configured_terms
    else:
        # Degraded/legacy clause without the playbook field: fall back to the
        # byte-identical in-code copy of the playbook list (drift-guarded by
        # tests/test_term_carveout_drift_guard.py) so a legitimate
        # data-protection / trade-secret carve-out is still honored.
        terms = list(DEFAULT_LONGER_SURVIVAL_CARVE_OUT_TERMS)
    return [
        re.escape(str(term).lower().strip()).replace(r"\ ", r"\s+")
        for term in terms
        if str(term).strip()
    ]


def _term_fragment_bounds(normalized: str, start: int, end: int) -> tuple[str, int, int]:
    # Scope the carve-out window at SENTENCE/CLAUSE granularity (``.`` and ``;``),
    # not at comma granularity. A legitimate longer-survival carve-out routinely
    # reads "...survive five years; with respect to trade secrets, ... in perpetuity"
    # or "...retained for as long as required by applicable law" -- the carve-out
    # term ("trade secrets", "applicable law") sits in a DIFFERENT comma-segment
    # from the perpetual trigger, so a comma-bounded window wrongly failed it.
    # Sentence bounds keep "with respect to trade secrets, ... in perpetuity"
    # together while still walling the trigger off from an unrelated ordinary-CI
    # perpetual rider that lives in a different sentence (precision preserved by
    # the directional scoping guards below, which reject ordinary-CI bleed).
    left_candidates = [
        normalized.rfind(separator, 0, start)
        for separator in (".", ";")
    ]
    right_candidates = [
        position
        for position in (normalized.find(separator, end) for separator in (".", ";"))
        if position != -1
    ]
    left = max(left_candidates) + 1
    right = min(right_candidates) if right_candidates else len(normalized)
    while left < right and normalized[left].isspace():
        left += 1
    while right > left and normalized[right - 1].isspace():
        right -= 1
    return normalized[left:right], start - left, end - left


def reason_code(clause: Mapping[str, Any], decision: str) -> str:
    semantic_code = _semantic_review_code(clause, decision)
    if semantic_code:
        return semantic_code
    analysis = clause.get("term_survival_analysis")
    if isinstance(analysis, dict):
        references = analysis.get("references", [])
        if isinstance(references, list) and references:
            for reference in references:
                if not isinstance(reference, dict):
                    continue
                if reference.get("unresolved_numbers"):
                    return "unresolved_survival_reference"
                if str(reference.get("status") or "") in {"partial", "unresolved"}:
                    return "unresolved_survival_reference"
                if reference.get("ordinary_confidentiality") is False:
                    return "survival_reference_scope_unclear"
            if decision == CLAUSE_DECISION_PASS:
                return "resolved_survival_reference_within_cap"
    reason = str(clause.get("reason") or clause.get("finding") or "").lower()
    issue = _issue_type(clause)
    if "indefinite" in reason:
        return "indefinite_survival"
    if "exceeds" in reason or "over" in reason or "longer than" in reason:
        return "term_survival_over_cap"
    if issue == "missing":
        return "missing_term_or_survival"
    if decision == CLAUSE_DECISION_REVIEW:
        return "unclear_term_or_survival"
    if decision == CLAUSE_DECISION_PASS:
        return "term_survival_within_cap"
    return _generic_reason_code(clause, decision)
