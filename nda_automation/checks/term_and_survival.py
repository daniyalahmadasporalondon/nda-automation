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
    term_paragraphs = _paragraph_matches(paragraphs, term_context_patterns)
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
        if digit_value:
            value = int(digit_value)
        elif parenthetical_digit:
            value = int(parenthetical_digit)
        elif word_value:
            value = _year_word_value(word_value)
        elif parenthetical_word:
            value = _year_word_value(parenthetical_word)
        else:
            continue
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
            if objects and not _ordinary_ci_subject_present(fragment[:relative_start]):
                object_alt = "|".join(re.escape(obj).replace(r"\ ", r"\s+") for obj in objects)
                # Allow up to two filler words ("a perpetual license", "remain
                # indefinitely available", "perpetual right to use") between the
                # indefinite word and the governed object.
                after = fragment[relative_end:]
                if re.match(rf"(?:\W+\w+){{0,2}}\W+(?:{object_alt})\b", after):
                    return True

        # --- CAPPED-DURATION demotion: "for so/as long as" ---
        if re.fullmatch(r"for\s+(?:so|as)\s+long\s+as", token):
            # (a) An explicit numeric period AFTER the connector (within its own
            #     governed clause -- "for as long as X, and for two (2) years")
            #     caps the survival, so the bare connector is not perpetual. The
            #     numeric term must FOLLOW the connector: a number that precedes it
            #     caps a DIFFERENT (prior) clause and must not launder an uncapped
            #     "...except trade secrets survive for so long as they remain secret"
            #     rider, so we look only at the text after the connector.
            if re.search(YEAR_TERM_PATTERN, fragment[relative_end:]):
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
    if re.search(ORDINARY_SURVIVAL_SUBJECT_PATTERN, before_term[last_carve_out.end():]):
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
                rf"(?:the\s+)?(?:applicable\s+)?{carve_out_pattern}\b",
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
        return _clause_term_patterns(clause, "indefinite_terms")
    return [_literal_word_pattern(term) for term in DEFAULT_INDEFINITE_TERMS if str(term).strip()]


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
