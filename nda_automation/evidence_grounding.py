"""Make every AI review finding grounded in the exact source text it cites.

A finding is *grounded* when it quotes supporting text that actually appears in
the reviewed document. Two findings are legitimately *ungroundable* by a quote:

* a required clause that is **missing** (decision ``fail`` / issue_type
  ``missing``) — there is no clause text to quote, the absence is the evidence;
* a prohibited clause that is **absent** (decision ``pass`` over a prohibited
  clause) — you cannot quote text that is not there.

Every other finding must cite at least one quote that resolves into the source.
A pass or fail that claims a decision without any groundable quote is *not*
trustworthy: it is downgraded to ``review`` and flagged so a human looks at it,
rather than silently shipping an ungrounded verdict.

This module is additive to the AI clause assessment contract: it derives an
explicit per-finding ``grounding`` status and a ``citation`` surface from the
already-validated ``evidence`` / ``structured_evidence`` so the Review tab and
redline can render ``based on: "<quoted text>"``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
    CLAUSE_DECISION_REVIEW,
)

GROUNDING_VERSION = 1

# A finding's grounding status.
GROUNDING_GROUNDED = "grounded"
GROUNDING_ABSENCE = "absence"
GROUNDING_UNGROUNDED = "ungrounded"
GROUNDING_STATUSES = (GROUNDING_GROUNDED, GROUNDING_ABSENCE, GROUNDING_UNGROUNDED)

# Issue type for a required clause that is wholly missing from the document.
ISSUE_TYPE_MISSING = "missing"
# Clause-type marker for prohibited clauses, whose absence is a legitimate pass.
CLAUSE_TYPE_PROHIBITED = "prohibited"

# Reason code stamped on a finding that was downgraded because it could not be
# grounded in the document text.
UNGROUNDED_REASON_CODE = "ungrounded_finding"

UNGROUNDED_REVIEW_REASON = (
    "This finding was escalated for human review because the AI assessment did "
    "not ground it in any quotable text from the document."
)


def classify_grounding(
    *,
    decision: str,
    issue_type: str,
    clause_type: str,
    structured_evidence: Sequence[Mapping[str, Any]],
) -> str:
    """Return the grounding status for a single finding.

    A finding is ``grounded`` when at least one structured-evidence record
    carries a quote that was matched into a source paragraph. The two
    quote-less verdicts above are ``absence``. Anything else is ``ungrounded``.
    """

    if _has_groundable_quote(structured_evidence):
        return GROUNDING_GROUNDED
    if _is_legitimate_absence(decision=decision, issue_type=issue_type, clause_type=clause_type):
        return GROUNDING_ABSENCE
    return GROUNDING_UNGROUNDED


def build_grounding(
    *,
    decision: str,
    issue_type: str,
    clause_type: str,
    structured_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the per-finding ``grounding`` object surfaced in the result."""

    records = [record for record in structured_evidence if isinstance(record, Mapping)]
    quoted = [record for record in records if _record_quote(record)]
    status = classify_grounding(
        decision=decision,
        issue_type=issue_type,
        clause_type=clause_type,
        structured_evidence=records,
    )
    return {
        "version": GROUNDING_VERSION,
        "status": status,
        "evidence_count": len(quoted),
        "requires_quote": status != GROUNDING_ABSENCE,
        "grounded": status == GROUNDING_GROUNDED,
    }


def build_citation(structured_evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Build the primary ``citation`` for a finding, or ``None`` if ungrounded.

    The citation is the first structured-evidence record that carries a quote.
    It exposes the exact quoted span and its document offsets so the Review tab
    and redline can render ``based on: "<quote>"`` and link to the source.
    """

    for record in structured_evidence:
        if not isinstance(record, Mapping):
            continue
        quote = _record_quote(record)
        if not quote:
            continue
        citation: dict[str, Any] = {
            "quote": quote,
            "paragraph_id": str(record.get("paragraph_id") or "").strip(),
        }
        span = _primary_span(record)
        if span is not None:
            start, end = span
            citation["start"] = start
            citation["end"] = end
        relevance = str(record.get("relevance") or "").strip()
        if relevance:
            citation["relevance"] = relevance
        return citation
    return None


def downgrade_ungrounded_finding(
    *,
    decision: str,
    issue_type: str,
    blocks_send: bool,
    reason_codes: Sequence[str],
) -> dict[str, Any]:
    """Return the fields that replace a finding the model failed to ground.

    A pass or fail with no groundable quote is downgraded to ``review`` so it
    blocks an automatic send and a human resolves it. ``review`` findings are
    left untouched — they already invite a human — but are still flagged.
    """

    existing_codes = [str(code).strip() for code in reason_codes if str(code).strip()]

    if decision == CLAUSE_DECISION_REVIEW:
        # Already inviting human judgement: keep the verdict and any specific
        # reason it carried (e.g. a missing-assessment code), appending the
        # ungrounded flag as a secondary signal rather than displacing it.
        cleaned_codes = list(existing_codes)
        if UNGROUNDED_REASON_CODE not in cleaned_codes:
            cleaned_codes.append(UNGROUNDED_REASON_CODE)
        return {
            "decision": CLAUSE_DECISION_REVIEW,
            "issue_type": issue_type,
            "blocks_send": True,
            "reason_codes": cleaned_codes,
            "downgraded": False,
        }

    # A pass/fail that the model could not ground is actively downgraded; the
    # ungrounded reason becomes the primary code so it leads the audit trail.
    cleaned_codes = [UNGROUNDED_REASON_CODE]
    cleaned_codes.extend(code for code in existing_codes if code != UNGROUNDED_REASON_CODE)
    return {
        "decision": CLAUSE_DECISION_REVIEW,
        "issue_type": "unclear",
        "blocks_send": True,
        "reason_codes": cleaned_codes,
        "downgraded": True,
        "downgraded_from": decision,
        "reason": UNGROUNDED_REVIEW_REASON,
    }


def _has_groundable_quote(structured_evidence: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(record, Mapping) and _record_quote(record)
        for record in structured_evidence
    )


def _is_legitimate_absence(*, decision: str, issue_type: str, clause_type: str) -> bool:
    normalized_decision = str(decision or "").strip().lower()
    normalized_issue = str(issue_type or "").strip().lower()
    normalized_type = str(clause_type or "").strip().lower()
    # A required clause that is simply missing: the absence is the finding.
    if normalized_decision == CLAUSE_DECISION_FAIL and normalized_issue == ISSUE_TYPE_MISSING:
        return True
    # A prohibited clause that does not appear: you cannot quote absent text.
    if normalized_decision == CLAUSE_DECISION_PASS and normalized_type == CLAUSE_TYPE_PROHIBITED:
        return True
    return False


def _record_quote(record: Mapping[str, Any]) -> str:
    quote = str(record.get("matched_text") or "").strip()
    if quote:
        terms = record.get("matched_terms")
        # ``matched_text`` falls back to the full paragraph when no quote was
        # cited; only count it as a quote when an explicit term backs it.
        if isinstance(terms, Sequence) and not isinstance(terms, (str, bytes)):
            if any(str(term).strip() for term in terms):
                return quote
    return ""


def _primary_span(record: Mapping[str, Any]) -> tuple[int, int] | None:
    spans = record.get("match_spans")
    if not isinstance(spans, Sequence) or isinstance(spans, (str, bytes)):
        return None
    for span in spans:
        if not isinstance(span, Mapping):
            continue
        start = span.get("start")
        end = span.get("end")
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            return start, end
    return None
