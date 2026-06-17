"""Post-review human approval gate and reviewer-decision state.

This module owns the *decision/approval* layer that sits on top of an AI review
result. It deliberately never reshapes the review-result payload: per-clause
reviewer decisions live in a matter-level ``reviewer_decisions`` map keyed by
clause id, and approval state lives in matter-level fields plus an append-only
``matter_timeline``. The review result (clauses, redlines, playbook provenance)
is treated as read-only here.

The reviewer resolves each clause the engine could not auto-pass, then approves
the matter. Approval is blocked while the review is stale (its playbook hash no
longer matches the published Playbook) or while any fail/review clause is still
unresolved.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .playbook_runtime import ensure_active_playbook_runtime
from .redline_actions import REDLINE_REPLACE_PARAGRAPH
from .review_staleness import CurrentRuntimeFn, review_result_staleness

CurrentPlaybookHashFn = Callable[[], str]

MATTER_STATUS_IN_REVIEW = "in_review"
MATTER_STATUS_APPROVED = "approved"

DECISION_ACCEPT = "accept"
DECISION_MODIFY = "modify"
DECISION_REJECT = "reject"
DECISION_COMMENT = "comment"
DECISION_ACTIONS = (DECISION_ACCEPT, DECISION_MODIFY, DECISION_REJECT, DECISION_COMMENT)

# Decisions whose proposed redline should be carried into the reviewed DOCX.
# A rejected or comment-only clause leaves the source text unchanged.
_DECISIONS_APPLYING_REDLINE = {DECISION_ACCEPT, DECISION_MODIFY}

# Clause decisions the engine produces that require a human to weigh in before
# the matter can be approved.
_UNRESOLVED_CLAUSE_DECISIONS = {"fail", "review"}

BLOCK_STALE_PLAYBOOK = "stale_playbook"
UNRESOLVED_CLAUSE_PREFIX = "unresolved_clause:"

MAX_DECISION_TEXT_CHARS = 20000
MAX_COMMENT_CHARS = 4000
MAX_ACTOR_CHARS = 240


class ReviewerDecisionError(ValueError):
    """Raised when a submitted reviewer decision is malformed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_reviewer_decision(payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Validate a submitted decision and return the stored ``reviewer_decision``.

    Raises ``ReviewerDecisionError`` with a caller-facing message when the action
    is unknown or a ``modify`` is missing its replacement text.
    """
    action = str(payload.get("action") or "").strip().lower()
    if action not in DECISION_ACTIONS:
        raise ReviewerDecisionError(
            "action must be one of: " + ", ".join(DECISION_ACTIONS) + "."
        )

    modified_text = payload.get("modified_text")
    if modified_text is not None and not isinstance(modified_text, str):
        raise ReviewerDecisionError("modified_text must be a string.")
    modified_text = (modified_text or "").strip()

    comment = payload.get("comment")
    if comment is not None and not isinstance(comment, str):
        raise ReviewerDecisionError("comment must be a string.")
    comment = " ".join((comment or "").split())[:MAX_COMMENT_CHARS]

    if action == DECISION_MODIFY and not modified_text:
        raise ReviewerDecisionError("modify decisions require modified_text.")
    if action == DECISION_COMMENT and not comment:
        raise ReviewerDecisionError("comment decisions require a comment.")

    decision: dict[str, Any] = {
        "action": action,
        "actor": str(actor or "").strip()[:MAX_ACTOR_CHARS] or "reviewer",
        "decided_at": _now_iso(),
    }
    if modified_text:
        decision["modified_text"] = modified_text[:MAX_DECISION_TEXT_CHARS]
    if comment:
        decision["comment"] = comment
    return decision


def matter_clauses(matter: dict[str, Any]) -> list[dict[str, Any]]:
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return []
    clauses = review_result.get("clauses")
    if not isinstance(clauses, list):
        return []
    return [clause for clause in clauses if isinstance(clause, dict)]


def find_clause(matter: dict[str, Any], clause_id: str) -> dict[str, Any] | None:
    for clause in matter_clauses(matter):
        if str(clause.get("id") or "") == clause_id:
            return clause
    return None


def reviewer_decisions(matter: dict[str, Any]) -> dict[str, Any]:
    decisions = matter.get("reviewer_decisions")
    if not isinstance(decisions, dict):
        return {}
    return {
        str(clause_id): decision
        for clause_id, decision in decisions.items()
        if isinstance(decision, dict)
    }


def clause_needs_decision(clause: dict[str, Any]) -> bool:
    return _clause_decision(clause) in _UNRESOLVED_CLAUSE_DECISIONS


def _clause_decision(clause: dict[str, Any]) -> str:
    return str(clause.get("decision") or "").strip().lower()


def resolution_summary(matter: dict[str, Any]) -> dict[str, Any]:
    """Summarize how many clauses needing a human decision still lack one.

    ``total`` counts clauses the engine flagged fail/review; ``resolved`` counts
    those that now carry a reviewer_decision; ``unresolved`` lists the clause ids
    still awaiting one (preserving review-result order).
    """
    decisions = reviewer_decisions(matter)
    needing = [
        str(clause.get("id") or "")
        for clause in matter_clauses(matter)
        if clause_needs_decision(clause) and str(clause.get("id") or "")
    ]
    unresolved = [clause_id for clause_id in needing if clause_id not in decisions]
    return {
        "total": len(needing),
        "resolved": len(needing) - len(unresolved),
        "unresolved": unresolved,
    }


def _current_published_playbook_hash() -> str:
    try:
        runtime = ensure_active_playbook_runtime()
    except Exception:  # Fail closed: an unreadable active playbook means stale.
        return ""
    return str(runtime.get("active_hash") or "")


def review_playbook_version_hash(review_result: object) -> str:
    """The cross-team contract field: review_result['playbook_version']['hash'].

    Stamped by backend-provenance at the engine choke point; byte-identical to
    playbook_runtime.active_hash. Empty when the review predates provenance
    stamping, in which case review_staleness still gates on playbook_runtime.
    """
    if not isinstance(review_result, dict):
        return ""
    playbook_version = review_result.get("playbook_version")
    if not isinstance(playbook_version, dict):
        return ""
    return str(playbook_version.get("hash") or "")


def review_is_stale(
    review_result: object,
    *,
    current_playbook_hash_func: CurrentPlaybookHashFn = _current_published_playbook_hash,
    current_runtime_func: CurrentRuntimeFn | None = None,
) -> bool:
    """Stale when review_staleness flags it OR playbook_version.hash drifts.

    review_staleness already hash-compares playbook_runtime; this additionally
    honors the locked playbook_version.hash field so the gate stays correct even
    if only that provenance field is present.

    ``current_runtime_func``/``current_playbook_hash_func`` are injectable so a
    batch caller (e.g. corpus_index.build_corpus) can resolve the active playbook
    runtime ONCE and thread the constant resolvers through every matter, instead
    of paying a playbook.json flock+read+validate per matter. When
    ``current_runtime_func`` is omitted the staleness check resolves the runtime
    itself (the unbatched default).
    """
    staleness = (
        review_result_staleness(review_result, current_runtime_func=current_runtime_func)
        if current_runtime_func is not None
        else review_result_staleness(review_result)
    )
    if staleness["stale"]:
        return True
    review_hash = review_playbook_version_hash(review_result)
    if not review_hash:
        return False
    current_hash = current_playbook_hash_func()
    return not current_hash or review_hash != current_hash


def approval_blocks(
    matter: dict[str, Any],
    *,
    current_playbook_hash_func: CurrentPlaybookHashFn | None = None,
    current_runtime_func: CurrentRuntimeFn | None = None,
) -> list[str]:
    """Return the reason codes that currently block approval (empty == approvable).

    The single human gate is "Approve Review": one approval signs off the whole
    matter, so per-clause reviewer decisions never block. The only blocker is
    ``stale_playbook`` -- a data-freshness guard raised when the stored review no
    longer matches the published Playbook (by review_staleness or the locked
    playbook_version.hash). As soon as the review is fresh, approval is allowed.

    ``current_playbook_hash_func``/``current_runtime_func`` are injectable so a
    batch caller can resolve the active playbook runtime ONCE and thread the
    constant resolvers through, avoiding a per-call playbook.json read.
    """
    kwargs: dict[str, Any] = {}
    if current_playbook_hash_func is not None:
        kwargs["current_playbook_hash_func"] = current_playbook_hash_func
    if current_runtime_func is not None:
        kwargs["current_runtime_func"] = current_runtime_func
    blocks: list[str] = []
    if review_is_stale(matter.get("review_result"), **kwargs):
        blocks.append(BLOCK_STALE_PLAYBOOK)
    return blocks


def approval_timeline_event(*, actor: str, blocks: list[str] | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "matter_approved",
        "actor": str(actor or "").strip()[:MAX_ACTOR_CHARS] or "reviewer",
        "at": _now_iso(),
    }
    if blocks:
        event["blocks_approval"] = list(blocks)
    return event


def public_reviewer_decision(decision: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(decision, dict):
        return None
    public = {
        "action": str(decision.get("action") or ""),
        "actor": str(decision.get("actor") or ""),
        "decided_at": str(decision.get("decided_at") or ""),
    }
    for key in ("modified_text", "comment"):
        value = decision.get(key)
        if isinstance(value, str) and value:
            public[key] = value
    return public


def public_clause_decision(matter: dict[str, Any], clause_id: str) -> dict[str, Any]:
    """The decision-endpoint clause view: the clause plus its reviewer_decision."""
    clause = find_clause(matter, clause_id) or {"id": clause_id}
    return {
        "id": str(clause.get("id") or clause_id),
        "decision": _clause_decision(clause),
        "clause_name": str(clause.get("clause_name") or clause.get("name") or ""),
        "needs_decision": clause_needs_decision(clause),
        "reviewer_decision": public_reviewer_decision(reviewer_decisions(matter).get(clause_id)),
    }


def reviewed_docx_payload(matter: dict[str, Any]) -> dict[str, Any]:
    """Build the redline-export payload that honors reviewer decisions.

    Accepted/modified clauses contribute their proposed redlines (modify
    overrides the replacement text); rejected and comment-only clauses are
    skipped so the source text is left intact. Comments become review comments.
    Returned as the kwargs ``redline_export_service.build_matter_redline``
    consumes (``export_redline_edits``/``manual_redline_edits``/``review_comments``).
    """
    decisions = reviewer_decisions(matter)
    review_result = matter.get("review_result") if isinstance(matter.get("review_result"), dict) else {}
    server_redlines = review_result.get("redline_edits") if isinstance(review_result.get("redline_edits"), list) else []

    redlines_by_clause: dict[str, list[dict[str, Any]]] = {}
    for redline in server_redlines:
        if not isinstance(redline, dict):
            continue
        redlines_by_clause.setdefault(str(redline.get("clause_id") or ""), []).append(redline)

    export_redline_edits: list[dict[str, Any]] = []
    manual_redline_edits: list[dict[str, Any]] = []
    review_comments: list[dict[str, Any]] = []

    for clause_id, decision in decisions.items():
        action = str(decision.get("action") or "").strip().lower()
        clause_redlines = redlines_by_clause.get(clause_id, [])

        if action in _DECISIONS_APPLYING_REDLINE:
            for redline in clause_redlines:
                export_redline_edits.append({
                    "id": str(redline.get("id") or ""),
                    "clause_id": clause_id,
                    "paragraph_id": str(redline.get("paragraph_id") or ""),
                    "action": str(redline.get("action") or ""),
                })
            if action == DECISION_MODIFY and str(decision.get("modified_text") or "").strip():
                manual_redline_edits.extend(
                    _manual_redline_for_modify(redline, str(decision.get("modified_text") or ""))
                    for redline in clause_redlines
                    if str(redline.get("paragraph_id") or "").strip()
                )

        comment = str(decision.get("comment") or "").strip()
        if comment:
            review_comments.append({
                "id": f"decision-{clause_id}",
                "clause_id": clause_id,
                "text": comment,
                "author": str(decision.get("actor") or "reviewer"),
                "scope": "clause",
            })

    return {
        "export_redline_edits": export_redline_edits,
        "manual_redline_edits": manual_redline_edits,
        "review_comments": review_comments,
    }


def _manual_redline_for_modify(redline: dict[str, Any], modified_text: str) -> dict[str, Any]:
    paragraph_id = str(redline.get("paragraph_id") or "")
    manual_redline: dict[str, Any] = {
        "id": f"modify-{str(redline.get('id') or paragraph_id)}",
        "action": REDLINE_REPLACE_PARAGRAPH,
        "paragraph_id": paragraph_id,
        "original_text": str(redline.get("original_text") or ""),
        "replacement_text": modified_text.strip()[:MAX_DECISION_TEXT_CHARS],
    }
    # Carry the source redline's block indexes onto the modify redline. The export
    # content-coverage gate keys expected-paragraph coverage on paragraph_index/
    # source_index (see docx_health._expected_redline_source_index); without them the
    # gate cannot tell which source block the modify replaces, so it keeps the original
    # text as expected, the export shows the modified text, and the legitimate edit is
    # rejected as a dropped/misplaced redline (HTTP 500). Copying the indexes lets the
    # gate compute the correct expectation for this intentional edit -- a genuinely
    # dropped redline still diverges and is still caught.
    for key in ("paragraph_index", "source_index"):
        value = redline.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            manual_redline[key] = value
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            manual_redline[key] = int(value.strip())
    return manual_redline
