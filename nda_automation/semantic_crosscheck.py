from __future__ import annotations

import re
from copy import deepcopy
from typing import Dict, Iterable, List, Tuple

from .checks.common import (
    ClauseResult,
    Paragraph,
    _match,
    is_circumvention_freedom_preserving,
)

SEMANTIC_CROSSCHECK_VERSION = 1
MAX_CROSSCHECK_RECORDS = 30

# The deterministic cross-check is a regex/lexicon pass that runs AFTER the
# clause checkers and BEFORE the AI/verifier overlay. It is paraphrase-fragile
# and polarity-blind (e.g. "Nothing herein shall restrict either Party from
# dealing with third parties" is freedom-preserving but pattern-matches a
# non-circ restriction). It must therefore NEVER mint a hard FAIL and NEVER
# auto-generate a redline edit that strips clause language -- the AI is the
# judge for these playbook signals. The strongest verdict it may emit is an
# escalation to human REVIEW, marked non-terminal so the AI/verifier verdict
# stands. See decision_arbiter (the SEMANTIC_CROSSCHECK_ESCALATION_KEY handoff).
SEMANTIC_CROSSCHECK_ESCALATION_KEY = "semantic_crosscheck_escalation"

RESTRICTION_PREFIX_PATTERN = (
    r"\b(?:shall|must|will|may|can)\s+not\b"
    r"|\b(?:agrees?|undertakes?|covenants?)\s+(?:not\s+|to\s+)?"
    r"|\b(?:is|are|be|remain(?:s)?)\s+(?:prohibited|restricted|barred|prevented)\b"
)
INTRODUCED_OBJECT_PATTERN = (
    r"(?:part(?:y|ies)|contacts?|customers?|counterpart(?:y|ies)|prospects?|"
    r"opportunit(?:y|ies)|business\s+relationships?|persons?|people|individuals?|"
    r"clients?|suppliers?|vendors?|investors?)"
)
# Deal-specific objects strong enough to signal non-circumvention even without a
# listed action verb. The action axis is the most paraphrasable, so we don't require
# it when the object itself is unambiguous commercial-relationship language (unlike
# "party", these don't appear in ordinary confidentiality boilerplate).
STRONG_INTRODUCED_OBJECT_PATTERN = (
    r"(?:opportunit(?:y|ies)|business\s+relationships?|prospects?)"
)
INTRODUCTION_SOURCE_PATTERN = (
    r"(?:introduced|surfaced|referred|presented|provided|identified|made\s+known)"
)
COMMERCIAL_CONTACT_ACTION_PATTERN = (
    r"(?:contact|communicat\w+|transact|deal|solicit|poach|approach|pursu\w+|"
    r"enter\s+into|engage\w*|work\s+with|partner\w*|do\s+business|"
    r"steer\s+clear\s+of|stay\s+away\s+from|divert\w*)"
)
NON_CIRCUMVENTION_SEMANTIC_PATTERNS = [
    (
        "introduced_contact_restriction",
        rf"(?:{RESTRICTION_PREFIX_PATTERN})[^.;\n]{{0,120}}\b{COMMERCIAL_CONTACT_ACTION_PATTERN}\b"
        rf"[^.;\n]{{0,120}}\b{INTRODUCED_OBJECT_PATTERN}\b[^.;\n]{{0,120}}\b{INTRODUCTION_SOURCE_PATTERN}\b",
    ),
    (
        "introduced_contact_restriction",
        rf"(?:{RESTRICTION_PREFIX_PATTERN})[^.;\n]{{0,120}}\b{COMMERCIAL_CONTACT_ACTION_PATTERN}\b"
        rf"[^.;\n]{{0,120}}\b{INTRODUCTION_SOURCE_PATTERN}\b[^.;\n]{{0,120}}\b{INTRODUCED_OBJECT_PATTERN}\b",
    ),
    (
        # Strong deal-specific object + introduction source, NO action verb required
        # (the action axis is the easiest to paraphrase around; these objects are
        # unambiguous enough that dropping it does not invite "party"-style FPs).
        "introduced_contact_restriction",
        rf"(?:{RESTRICTION_PREFIX_PATTERN})[^.;\n]{{0,140}}\b{STRONG_INTRODUCED_OBJECT_PATTERN}\b"
        rf"[^.;\n]{{0,80}}\b{INTRODUCTION_SOURCE_PATTERN}\b",
    ),
    (
        "introduced_contact_restriction",
        rf"(?:{RESTRICTION_PREFIX_PATTERN})[^.;\n]{{0,80}}\b{INTRODUCTION_SOURCE_PATTERN}\b"
        rf"[^.;\n]{{0,80}}\b{STRONG_INTRODUCED_OBJECT_PATTERN}\b",
    ),
    (
        "bypass_restriction",
        rf"(?:{RESTRICTION_PREFIX_PATTERN})[^.;\n]{{0,80}}\bbypass\w*\b"
        r"[^.;\n]{0,80}\b(?:company|disclosing\s+party|introducing\s+party|provider)\b",
    ),
    (
        "exclusive_dealing_restriction",
        r"\b(?:agrees?|undertakes?|covenants?)\s+to\s+deal\s+exclusiv\w+\s+with\b"
        r"|(?:\bshall\b|\bmust\b|\bwill\b)[^.;\n]{0,80}\bdeal\s+exclusiv\w+\s+with\b",
    ),
]

CONFIDENTIAL_INFORMATION_EXCLUSION_CONTEXT_PATTERN = (
    r"\b(?:does\s+not\s+include|shall\s+not\s+include|not\s+include|excludes?|"
    r"excluded\s+from\s+confidential\s+information|is\s+not\s+confidential\s+information)\b"
)
CONFIDENTIAL_INFORMATION_USAGE_RIGHT_PATTERN = (
    r"\b(?:may|can)\s+(?:(?:freely|directly|unrestrictedly|without\s+(?:restriction|limitation|limit)|"
    r"for\s+any\s+purpose)\s+){0,3}(?:use|retain|disclose|exploit)\b"
    r"|\b(?:free|permitted|allowed|entitled)\s+to\s+(?:use|retain|disclose|exploit)\b"
    r"|\b(?:has|have)\s+(?:the\s+)?right\s+to\s+(?:use|retain|disclose|exploit)\b"
)
INDEPENDENT_DEVELOPMENT_SEMANTIC_PATTERN = (
    r"\b(?:independent(?:ly)?\s+(?:develop\w+|creat\w+|deriv\w+|discover\w+|sourc\w+)|"
    r"(?:develop\w+|creat\w+|deriv\w+|discover\w+|sourc\w+)\s+independently)\b"
)
INDEPENDENT_DEVELOPMENT_QUALIFICATION_PATTERN = (
    r"\bwithout\s+(?:use|using|access|reference|reliance|recourse|regard|knowledge|the\s+(?:use|aid|benefit))\b"
    r"|\b(?:no|without)\s+access\s+to\b"
    r"|\bhad\s+no\s+(?:access|knowledge|reference|recourse)\b"
    r"|\bdid\s+not\s+(?:use|access|reference|rely|receive)\b"
    r"|\bnot\s+(?:derived|based)\s+(?:from|on|upon)\b"
    r"|\bindependent(?:ly)?\s+of\b"
)
QUALIFICATION_WINDOW = 160


def apply_semantic_crosscheck(
    *,
    clause_results: List[ClauseResult],
    clauses_by_id: Dict[str, Dict[str, object]],
    paragraphs: List[Paragraph],
) -> Tuple[List[ClauseResult], Dict[str, object]]:
    """Run an independent deterministic semantic pass over checker outputs.

    This is deliberately separate from the clause checkers. It does not try to
    prove a clause clean. It only escalates a clean result to review/fail, or
    converts a checker/cross-check disagreement into review.
    """
    updated_results = [deepcopy(result) for result in clause_results]
    result_positions = {
        str(result.get("id") or ""): index
        for index, result in enumerate(updated_results)
        if result.get("id")
    }
    records: List[Dict[str, object]] = []

    _apply_confidential_information_crosscheck(updated_results, result_positions, clauses_by_id, paragraphs, records)
    _apply_non_circumvention_crosscheck(updated_results, result_positions, clauses_by_id, paragraphs, records)

    return updated_results, {
        "version": SEMANTIC_CROSSCHECK_VERSION,
        "record_count": len(records),
        "records": records[:MAX_CROSSCHECK_RECORDS],
    }


def _apply_confidential_information_crosscheck(
    results: List[ClauseResult],
    positions: Dict[str, int],
    clauses_by_id: Dict[str, Dict[str, object]],
    paragraphs: List[Paragraph],
    records: List[Dict[str, object]],
) -> None:
    current = _result_for_clause(results, positions, "confidential_information")
    clause = clauses_by_id.get("confidential_information")
    if current is None or clause is None or not _clause_currently_clean(current):
        return

    explicit_exclusion_paragraphs: List[Paragraph] = []
    usage_right_review_paragraphs: List[Paragraph] = []
    signal_records: List[Dict[str, object]] = []
    for paragraph in paragraphs:
        text = str(paragraph.get("text") or "")
        if not re.search(INDEPENDENT_DEVELOPMENT_SEMANTIC_PATTERN, text, flags=re.IGNORECASE):
            continue
        if _independent_development_is_qualified(text):
            continue
        if re.search(CONFIDENTIAL_INFORMATION_EXCLUSION_CONTEXT_PATTERN, text, flags=re.IGNORECASE):
            explicit_exclusion_paragraphs.append(paragraph)
            # Review, not fail: the regex flags a *suspect* unqualified exclusion;
            # the AI decides whether it actually weakens the definition.
            signal_records.append(_crosscheck_record("confidential_information", paragraph, "unqualified_independent_development_exclusion", "review"))
        elif re.search(CONFIDENTIAL_INFORMATION_USAGE_RIGHT_PATTERN, text, flags=re.IGNORECASE):
            usage_right_review_paragraphs.append(paragraph)
            signal_records.append(_crosscheck_record("confidential_information", paragraph, "unqualified_independent_development_usage_right", "review"))

    if explicit_exclusion_paragraphs:
        replacement = _review_escalation(
            clause,
            explicit_exclusion_paragraphs,
            reason=(
                "Semantic cross-check flagged a possible unqualified independent-development "
                "exclusion for human review."
            ),
            what_to_fix=(
                "Confirm whether this is an unqualified independent-development exclusion that should "
                "be removed or limited to material developed without use of or reference to "
                "Confidential Information."
            ),
        )
        _carry_forward_context(replacement, current)
        _attach_confidential_information_crosscheck_analysis(replacement, current, explicit_exclusion_paragraphs, [], signal_records)
        _replace_result(results, positions, "confidential_information", replacement)
        records.extend(signal_records)
        return

    if usage_right_review_paragraphs:
        replacement = _review_escalation(
            clause,
            usage_right_review_paragraphs,
            reason=(
                "Semantic cross-check found independent-development usage-right language that may "
                "weaken confidentiality protections."
            ),
            what_to_fix=(
                "Confirm whether the usage-right language creates an unqualified independent-development "
                "carve-out or permission to use non-confidential material beyond the standard exclusions."
            ),
        )
        _carry_forward_context(replacement, current)
        _attach_confidential_information_crosscheck_analysis(replacement, current, [], usage_right_review_paragraphs, signal_records)
        _replace_result(results, positions, "confidential_information", replacement)
        records.extend(signal_records)


def _apply_non_circumvention_crosscheck(
    results: List[ClauseResult],
    positions: Dict[str, int],
    clauses_by_id: Dict[str, Dict[str, object]],
    paragraphs: List[Paragraph],
    records: List[Dict[str, object]],
) -> None:
    current = _result_for_clause(results, positions, "non_circumvention")
    clause = clauses_by_id.get("non_circumvention")
    if current is None or clause is None or not _clause_currently_clean(current):
        return

    suspect_paragraphs: List[Paragraph] = []
    signal_records: List[Dict[str, object]] = []
    for paragraph in paragraphs:
        text = str(paragraph.get("text") or "")
        classification = _non_circumvention_semantic_classification(text)
        if not classification:
            continue
        suspect_paragraphs.append(paragraph)
        # Emitted as a review signal, never a fail: the regex flags a *suspect*
        # pattern; the AI/verifier decides whether it is a real restriction.
        signal_records.append(_crosscheck_record("non_circumvention", paragraph, classification, "review"))

    if not suspect_paragraphs:
        return

    replacement = _review_escalation(
        clause,
        suspect_paragraphs,
        reason=(
            "Semantic cross-check flagged possible non-circumvention, introduced-contact, "
            "or exclusivity language for human review; the AI is the judge of whether it "
            "is an actual prohibited restriction."
        ),
        what_to_fix=(
            "Confirm whether this language imposes a non-circumvention, introduced-contact "
            "non-solicit, direct-dealing, bypass, or exclusivity restriction that must be removed, "
            "or is merely a freedom-preserving carve-out that needs no change."
        ),
    )
    _carry_forward_context(replacement, current)
    _attach_non_circumvention_crosscheck_analysis(replacement, current, suspect_paragraphs, signal_records)
    _replace_result(results, positions, "non_circumvention", replacement)
    records.extend(signal_records)


def _review_escalation(
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    *,
    reason: str,
    what_to_fix: str,
) -> ClauseResult:
    """Build a non-terminal REVIEW escalation result for the cross-check.

    Built from ``_match`` (status="match", issue_type="none") so it can never be
    mistaken for a ``present_but_wrong`` check -- which is what
    ``clause_outcomes._is_present_but_wrong_check`` keys on to auto-generate a
    redline edit. The explicit ``decision="review"`` makes the deterministic
    snapshot a REVIEW (not a FAIL), and ``SEMANTIC_CROSSCHECK_ESCALATION_KEY``
    marks it as cross-check-sourced so ``decision_arbiter`` keeps it non-terminal:
    the AI/verifier may clear it back to PASS, but absent an AI clearance it
    stays at human REVIEW.
    """
    replacement = _match(clause, reason, paragraphs)
    replacement["decision"] = "review"
    replacement["needs_review"] = True
    replacement["review_reason"] = reason
    replacement["decision_reason"] = reason
    replacement["what_to_fix"] = what_to_fix
    replacement[SEMANTIC_CROSSCHECK_ESCALATION_KEY] = True
    return replacement


def _non_circumvention_semantic_classification(text: str) -> str:
    if is_circumvention_freedom_preserving(text):
        return ""
    for classification, pattern in NON_CIRCUMVENTION_SEMANTIC_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return classification
    return ""


def _independent_development_is_qualified(text: str) -> bool:
    for match in re.finditer(INDEPENDENT_DEVELOPMENT_SEMANTIC_PATTERN, text, flags=re.IGNORECASE):
        start = max(0, match.start() - QUALIFICATION_WINDOW)
        end = min(len(text), match.end() + QUALIFICATION_WINDOW)
        context = text[start:end]
        if re.search(INDEPENDENT_DEVELOPMENT_QUALIFICATION_PATTERN, context, flags=re.IGNORECASE):
            return True
    return False


def _clause_currently_clean(result: ClauseResult) -> bool:
    if result.get("needs_review"):
        return False
    explicit_decision = str(result.get("decision") or "").strip().lower()
    if explicit_decision in {"review", "fail"}:
        return False
    return bool(result.get("passes"))


def _result_for_clause(results: List[ClauseResult], positions: Dict[str, int], clause_id: str) -> ClauseResult | None:
    position = positions.get(clause_id)
    if position is None:
        return None
    return results[position]


def _replace_result(
    results: List[ClauseResult],
    positions: Dict[str, int],
    clause_id: str,
    replacement: ClauseResult,
) -> None:
    position = positions.get(clause_id)
    if position is None:
        return
    results[position] = replacement


def _carry_forward_context(replacement: ClauseResult, current: ClauseResult) -> None:
    for key in ["structure_context"]:
        if key in current:
            replacement[key] = deepcopy(current[key])
    replacement["semantic_crosscheck"] = True


def _attach_confidential_information_crosscheck_analysis(
    replacement: ClauseResult,
    current: ClauseResult,
    explicit_exclusion_paragraphs: Iterable[Paragraph],
    usage_right_review_paragraphs: Iterable[Paragraph],
    signal_records: List[Dict[str, object]],
) -> None:
    analysis = dict(current.get("confidential_information_analysis") or {})
    # Both the unqualified-exclusion and usage-right signals are now review-only
    # escalations (the regex flags suspects; the AI judges). They land in the
    # review bucket (rendered as review_evidence), not the check/fail bucket.
    analysis.setdefault("explicit_problematic_exclusion_paragraph_ids", [])
    analysis["usage_right_review_paragraph_ids"] = _merged_ids(
        analysis.get("usage_right_review_paragraph_ids", []),
        [*explicit_exclusion_paragraphs, *usage_right_review_paragraphs],
    )
    replacement["confidential_information_analysis"] = analysis
    replacement["semantic_crosscheck_analysis"] = {
        "triggered": True,
        "records": signal_records,
    }


def _attach_non_circumvention_crosscheck_analysis(
    replacement: ClauseResult,
    current: ClauseResult,
    suspect_paragraphs: Iterable[Paragraph],
    signal_records: List[Dict[str, object]],
) -> None:
    analysis = dict(current.get("non_circumvention_analysis") or {})
    # Cross-check escalations are review-only signals, never prohibited fails, so
    # they land in review_paragraph_ids (rendered as review_evidence downstream).
    analysis["review_paragraph_ids"] = _merged_ids(
        analysis.get("review_paragraph_ids", []),
        suspect_paragraphs,
    )
    analysis.setdefault("prohibited_paragraph_ids", [])
    analysis.setdefault("lawful_circumvention_paragraph_ids", [])
    analysis.setdefault("negated_reference_paragraph_ids", [])
    existing_signal_records = analysis.get("signal_records", [])
    if not isinstance(existing_signal_records, list):
        existing_signal_records = []
    analysis["signal_records"] = [
        *existing_signal_records,
        *[
            {
                "paragraph_id": record["paragraph_id"],
                "matched_pattern_count": 1,
                "classification": "review",
                "semantic_crosscheck_classification": record["classification"],
            }
            for record in signal_records
        ],
    ]
    replacement["non_circumvention_analysis"] = analysis
    replacement["semantic_crosscheck_analysis"] = {
        "triggered": True,
        "records": signal_records,
    }


def _merged_ids(existing: object, paragraphs: Iterable[Paragraph]) -> List[str]:
    values: List[str] = []
    if isinstance(existing, list):
        values.extend(str(value) for value in existing if str(value).strip())
    values.extend(str(paragraph.get("id") or "") for paragraph in paragraphs if paragraph.get("id"))
    deduped: List[str] = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def _crosscheck_record(clause_id: str, paragraph: Paragraph, classification: str, outcome: str) -> Dict[str, object]:
    return {
        "clause_id": clause_id,
        "paragraph_id": str(paragraph.get("id") or ""),
        "paragraph_index": paragraph.get("index"),
        "classification": classification,
        "outcome": outcome,
        "text": str(paragraph.get("text") or ""),
    }
