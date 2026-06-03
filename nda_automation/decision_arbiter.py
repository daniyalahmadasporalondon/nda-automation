"""Single owner of clause verdict precedence.

Before this module, the final decision was an implicit "last writer wins":
checkers, the semantic fallback, the semantic crosscheck, and the AI overlay
each mutated `clause["decision"]`/`needs_review` in pipeline order, and
`checker._clause_decision` read whatever survived. That made the verdict depend
on call order and let the AI overlay silently soften a deterministic fail.

The arbiter makes precedence explicit and call-order independent:

  1. deterministic fail            -> fail      (AI dissent recorded, never acted on)
  2. deterministic review          -> review    (AI dissent recorded)
  3. deterministic pass + AI escalation (disagreement / low confidence /
     invalid citation)             -> review    (source: ai -- the recall net)
  4. deterministic pass + AI confirm / error / absent -> pass
  5. unknown                       -> review

"Deterministic" = the checkers plus the deterministic crosscheck/fallback
layers (all regex/lexicon); their fails are real prohibited-pattern matches and
stay fails. The fail-floor applies only to the AI overlay, which is the
non-deterministic layer: it may escalate a pass to review, but it may never
soften a deterministic review or fail. The AI's dissent is still recorded on the
clause (and in the audit trace) so a reviewer sees "Python failed this clause;
AI disagreed" without the product auto-clearing or softening the issue.
"""
from __future__ import annotations

from typing import Dict, Optional

from .review_state import CLAUSE_DECISION_FAIL, CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW

SEMANTIC_REVIEW_THRESHOLD = 0.75

# AI verdict statuses that escalate a deterministic PASS to review.
AI_ESCALATION_STATUSES = frozenset({"disagreement", "low_confidence", "invalid"})

_DECISIONS = {CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW, CLAUSE_DECISION_FAIL}


def semantic_confidence(clause: Dict[str, object]) -> Optional[float]:
    value = clause.get("semantic_confidence")
    if value is None:
        value = clause.get("confidence")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_deterministic_signal(clause: Dict[str, object]) -> bool:
    if str(clause.get("decision") or "").strip().lower() in _DECISIONS:
        return True
    return any(key in clause for key in ("needs_review", "passes", "semantic_confidence", "confidence"))


def deterministic_decision(clause: Dict[str, object]) -> str:
    """The base verdict from the deterministic layers, independent of AI.

    Mirrors the previous checker._clause_decision rule, but is meant to be read
    from clause fields the AI overlay no longer touches.
    """
    explicit = str(clause.get("decision") or "").strip().lower()
    if explicit in _DECISIONS:
        return explicit
    if clause.get("needs_review"):
        return CLAUSE_DECISION_REVIEW
    confidence = semantic_confidence(clause)
    if confidence is not None and confidence < SEMANTIC_REVIEW_THRESHOLD:
        return CLAUSE_DECISION_REVIEW
    if not clause.get("passes"):
        return CLAUSE_DECISION_FAIL
    return CLAUSE_DECISION_PASS


def arbitrate(clause: Dict[str, object]) -> Dict[str, object]:
    """Return the final {decision, source, reason_code?, reason?} for a clause.

    Reads the snapshot ``deterministic_decision`` (taken after the deterministic
    layers, before AI) when present, falling back to deriving it from clause
    fields. AI escalation is read from ``ai_review_analysis``.
    """
    det = str(clause.get("deterministic_decision") or "").strip().lower()
    if det not in _DECISIONS:
        if not _has_deterministic_signal(clause):
            # No usable verdict at all -> fail safe to human review, never silently pass.
            return {"decision": CLAUSE_DECISION_REVIEW, "source": "arbiter_default"}
        det = deterministic_decision(clause)

    analysis = clause.get("ai_review_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    ai_status = str(analysis.get("status") or "").strip().lower()

    if det == CLAUSE_DECISION_FAIL:
        return {"decision": CLAUSE_DECISION_FAIL, "source": "deterministic"}
    if det == CLAUSE_DECISION_REVIEW:
        return {"decision": CLAUSE_DECISION_REVIEW, "source": "deterministic"}
    if det == CLAUSE_DECISION_PASS:
        if ai_status in AI_ESCALATION_STATUSES:
            return {
                "decision": CLAUSE_DECISION_REVIEW,
                "source": "ai",
                "reason_code": str(analysis.get("reason_code") or "ai_semantic_review"),
                "reason": str(analysis.get("reason") or ""),
            }
        return {"decision": CLAUSE_DECISION_PASS, "source": "deterministic"}
    return {"decision": CLAUSE_DECISION_REVIEW, "source": "arbiter_default"}
