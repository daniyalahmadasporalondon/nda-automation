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

# Honest caveat appended after a downgraded finding's own substantive concern. It
# states the AI could not tie its read to a specific quote (NOT that the document
# lacks the text) and tells the reviewer what to do next.
UNGROUNDED_REVIEW_CAVEAT = (
    "The AI could not tie this to a specific quote in the document, so it could not "
    "be auto-verified and needs human review. Confirm against the clause text before sending."
)

# Standalone reason used when a downgraded finding carried no substantive text to lead with.
UNGROUNDED_REVIEW_REASON = (
    "The AI flagged this clause for review but did not cite a specific quote to support "
    "it, so it needs human review. Confirm against the clause text in the document before sending."
)


def ungrounded_review_reason(substantive_reason: str = "") -> str:
    """Compose the reviewer-facing reason for a finding downgraded for lack of grounding.

    Leads with the model's own substantive concern (so the reviewer sees WHAT the AI
    thought) and appends an honest caveat that it could not be tied to a quote. Falls
    back to a standalone caveat when no substance survived. The wording deliberately
    never claims the document lacks the text -- it only states the AI did not cite a
    quote, which is the actual failure being surfaced.
    """

    substantive = " ".join((substantive_reason or "").split()).strip()
    if not substantive or substantive == UNGROUNDED_REVIEW_REASON:
        return UNGROUNDED_REVIEW_REASON
    if substantive[-1] not in ".!?":
        substantive += "."
    return f"{substantive} {UNGROUNDED_REVIEW_CAVEAT}"


def classify_grounding(
    *,
    decision: str,
    issue_type: str,
    clause_type: str,
    structured_evidence: Sequence[Mapping[str, Any]],
    decision_source: str = "",
) -> str:
    """Return the grounding status for a single finding.

    A finding is ``grounded`` when at least one structured-evidence record
    carries a quote that was matched into a source paragraph. The two
    quote-less verdicts above are ``absence``. Anything else is ``ungrounded``.

    ``decision_source`` lets the AI verifier mark a finding it owns: when it
    refutes a clause to a pass it deliberately clears the disproven evidence, so
    that empty-evidence verdict is a legitimate absence regardless of clause type.
    """

    if _has_groundable_quote(structured_evidence):
        return GROUNDING_GROUNDED
    if _is_legitimate_absence(
        decision=decision,
        issue_type=issue_type,
        clause_type=clause_type,
        decision_source=decision_source,
    ):
        return GROUNDING_ABSENCE
    return GROUNDING_UNGROUNDED


def build_grounding(
    *,
    decision: str,
    issue_type: str,
    clause_type: str,
    structured_evidence: Sequence[Mapping[str, Any]],
    decision_source: str = "",
) -> dict[str, Any]:
    """Build the per-finding ``grounding`` object surfaced in the result."""

    records = [record for record in structured_evidence if isinstance(record, Mapping)]
    quoted = [record for record in records if _record_quote(record)]
    status = classify_grounding(
        decision=decision,
        issue_type=issue_type,
        clause_type=clause_type,
        structured_evidence=records,
        decision_source=decision_source,
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


# A clause whose decision was rewritten by the AI verifier owns its evidence
# state intentionally: a verifier refute-to-pass deliberately CLEARS the
# disproven evidence, so an empty-evidence verifier pass is legitimate, not
# ungrounded. Grounding treats such a pass as an absence rather than re-flagging
# it. See [[ai-verifier-pass-design]] for the composition contract.
VERIFIER_DECISION_SOURCE = "ai_verifier"


def refinalize_clause_grounding(clause: dict[str, Any]) -> dict[str, Any]:
    """Recompute ``grounding`` + ``citation`` on a clause edited after grounding.

    The AI verifier runs after grounding and may rewrite a clause's decision and
    clear its evidence. That leaves the clause carrying a stale ``grounding`` /
    ``citation`` from the first pass, describing evidence that no longer exists.
    Call this on each verifier-changed clause (right after its
    ``structured_evidence`` is rebuilt, before aggregation) to re-derive a
    consistent grounding surface from the clause's CURRENT state.

    Mutates and returns ``clause``. A clause that is no longer grounded loses its
    ``citation`` so nothing dangles a quote the finding no longer relies on.
    """

    if not isinstance(clause, Mapping):
        return clause

    decision = str(clause.get("decision") or "").strip().lower()
    issue_type = str(clause.get("issue_type") or "").strip().lower()
    clause_type = str(clause.get("type") or "").strip().lower()
    decision_source = str(clause.get("decision_source") or "")
    structured_evidence = clause.get("structured_evidence")
    if not isinstance(structured_evidence, Sequence) or isinstance(structured_evidence, (str, bytes)):
        structured_evidence = []

    # ``decision_source`` carries the verifier marker, so a verifier-cleared pass
    # of any clause type resolves to a legitimate absence (handled uniformly in
    # _is_legitimate_absence) rather than being re-flagged as ungrounded.
    clause["grounding"] = build_grounding(
        decision=decision,
        issue_type=issue_type,
        clause_type=clause_type,
        structured_evidence=structured_evidence,
        decision_source=decision_source,
    )
    citation = build_citation(structured_evidence)
    if citation is not None:
        clause["citation"] = citation
    else:
        clause.pop("citation", None)
    return clause


def downgrade_ungrounded_finding(
    *,
    decision: str,
    issue_type: str,
    blocks_send: bool,
    reason_codes: Sequence[str],
    substantive_reason: str = "",
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
        "reason": ungrounded_review_reason(substantive_reason),
    }


def _has_groundable_quote(structured_evidence: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(record, Mapping) and _record_quote(record)
        for record in structured_evidence
    )


def _is_legitimate_absence(
    *,
    decision: str,
    issue_type: str,
    clause_type: str,
    decision_source: str = "",
) -> bool:
    normalized_decision = str(decision or "").strip().lower()
    normalized_issue = str(issue_type or "").strip().lower()
    normalized_type = str(clause_type or "").strip().lower()
    # A required clause that is simply missing: the absence is the finding.
    if normalized_decision == CLAUSE_DECISION_FAIL and normalized_issue == ISSUE_TYPE_MISSING:
        return True
    # A prohibited clause that does not appear: you cannot quote absent text.
    if normalized_decision == CLAUSE_DECISION_PASS and normalized_type == CLAUSE_TYPE_PROHIBITED:
        return True
    # A clause the AI verifier owns: a refute-to-pass deliberately clears the
    # disproven evidence, so an empty-evidence verifier verdict is a legitimate
    # absence for any clause type, not an ungrounded finding to re-downgrade.
    if str(decision_source or "") == VERIFIER_DECISION_SOURCE:
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
