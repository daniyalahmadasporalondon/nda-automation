"""Explain WHY each proposed redline is being suggested.

A redline edit tells the reviewer *what* to change; the rationale tells them
*why*. For every clause that produces a redline, we attach a
``redline_rationale`` to the clause result:

    redline_rationale = {
        "explanation": str,            # why this edit, in plain language
        "basis": {                     # the source text the edit responds to
            "quote": str,
            "paragraph_id": str,
        },
    }

The explanation is sourced from the Playbook itself — the clause requirement,
its dynamic ``fallback`` wording / clause ``instructions``, and the redline
action — not from anything the model invented. The basis is the clause's own
grounded citation (the quote the finding cited), so the reviewer can see exactly
which language in the document the edit is reacting to. A redline that quotes no
source text (e.g. inserting a missing required clause) carries an empty quote and
the anchor/matched paragraph id when one exists.

This module is additive and shared by both review engines (deterministic
``review_nda`` and the AI-first ``build_ai_first_review_result``); both call
``attach_redline_rationales`` once they have their clause results and the
redline edits derived from them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

REDLINE_RATIONALE_VERSION = 1

_ACTION_INTENT = {
    REDLINE_REPLACE_PARAGRAPH: "replace the current language",
    REDLINE_INSERT_AFTER_PARAGRAPH: "add the missing language",
    REDLINE_DELETE_PARAGRAPH: "remove the offending language",
}


def attach_redline_rationales(
    clause_results: Sequence[MutableClause],
    redline_edits: Sequence[Mapping[str, Any]],
    *,
    playbook_clauses_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Stamp ``redline_rationale`` on every clause that produced a redline.

    Clauses without a redline edit are left untouched (no key added). The first
    edit for a clause defines its action; a clause whose redline is later removed
    by a verifier downgrade simply never gets a rationale because no edit names it.
    Mutates ``clause_results`` in place.
    """
    edits_by_clause = _edits_by_clause_id(redline_edits)
    clauses_by_id = dict(playbook_clauses_by_id or {})
    for clause in clause_results:
        if not isinstance(clause, Mapping):
            continue
        clause_id = str(clause.get("id") or "")
        edit = edits_by_clause.get(clause_id)
        if edit is None:
            continue
        playbook_clause = clauses_by_id.get(clause_id)
        clause["redline_rationale"] = build_redline_rationale(clause, edit, playbook_clause)


def build_redline_rationale(
    clause: Mapping[str, Any],
    edit: Mapping[str, Any],
    playbook_clause: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``redline_rationale`` object for one clause + its redline edit."""
    action = str(edit.get("action") or "").strip()
    return {
        "version": REDLINE_RATIONALE_VERSION,
        "action": action,
        "explanation": _explanation(clause, action, playbook_clause),
        "basis": _basis(clause, edit),
    }


def _explanation(
    clause: Mapping[str, Any],
    action: str,
    playbook_clause: Mapping[str, Any] | None,
) -> str:
    intent = _ACTION_INTENT.get(action, "revise the language")
    requirement = _playbook_text(clause, playbook_clause, "requirement")
    instructions = _instructions_text(clause, playbook_clause)
    fallback_wording = _fallback_wording(clause, playbook_clause)

    sentences: list[str] = []
    if requirement:
        sentences.append(f"The Playbook requires that {_lower_first(_strip_period(requirement))}.")
    else:
        sentences.append("The Playbook position is not satisfied by the current clause.")

    lead = f"This redline proposes to {intent}"
    if action == REDLINE_DELETE_PARAGRAPH:
        sentences.append(f"{lead} because the document contains a restriction the Playbook prohibits.")
    elif fallback_wording:
        sentences.append(f"{lead} using the Playbook's fallback wording.")
    else:
        sentences.append(f"{lead} to bring the clause in line with the Playbook position.")

    if instructions:
        sentences.append(f"Playbook guidance: {_strip_period(instructions)}.")

    detail = _supporting_detail(clause)
    if detail:
        sentences.append(detail)

    return " ".join(sentence for sentence in sentences if sentence)


def _supporting_detail(clause: Mapping[str, Any]) -> str:
    # Prefer the concrete fix the finding already produced; fall back to its
    # human-facing reason so the rationale always carries the clause's own context.
    what_to_fix = _text(clause.get("what_to_fix"))
    if what_to_fix and what_to_fix.lower() not in _GENERIC_FIX_TEXT:
        return f"Suggested fix: {_strip_period(what_to_fix)}."
    reason = _text(clause.get("reason")) or _text(clause.get("decision_reason"))
    if reason:
        return _ensure_period(reason)
    return ""


def _basis(clause: Mapping[str, Any], edit: Mapping[str, Any]) -> dict[str, str]:
    citation = clause.get("citation")
    if isinstance(citation, Mapping):
        quote = _text(citation.get("quote"))
        paragraph_id = _text(citation.get("paragraph_id"))
        if quote or paragraph_id:
            return {"quote": quote, "paragraph_id": paragraph_id}

    # No grounded citation (e.g. inserting a missing clause): fall back to the
    # paragraph the edit anchors on / replaces so the basis still points at the
    # document location the edit touches, with the original text as the quote.
    paragraph_id = _text(edit.get("paragraph_id"))
    quote = _text(edit.get("original_text")) or _text(edit.get("anchor_text"))
    return {"quote": quote, "paragraph_id": paragraph_id}


def _edits_by_clause_id(redline_edits: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    edits: dict[str, Mapping[str, Any]] = {}
    for edit in redline_edits:
        if not isinstance(edit, Mapping):
            continue
        clause_id = str(edit.get("clause_id") or "")
        if clause_id and clause_id not in edits:
            edits[clause_id] = edit
    return edits


def _instructions_text(
    clause: Mapping[str, Any],
    playbook_clause: Mapping[str, Any] | None,
) -> str:
    for source in (clause, playbook_clause):
        if not isinstance(source, Mapping):
            continue
        raw = source.get("instructions")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            parts = [_text(item) for item in raw if _text(item)]
            if parts:
                return "; ".join(parts)
    return ""


def _fallback_wording(
    clause: Mapping[str, Any],
    playbook_clause: Mapping[str, Any] | None,
) -> str:
    for source in (clause, playbook_clause):
        if not isinstance(source, Mapping):
            continue
        fallback = source.get("fallback")
        if isinstance(fallback, Mapping):
            wording = _text(fallback.get("wording"))
            if wording:
                return wording
    return ""


def _playbook_text(
    clause: Mapping[str, Any],
    playbook_clause: Mapping[str, Any] | None,
    field: str,
) -> str:
    text = _text(clause.get(field))
    if text:
        return text
    if isinstance(playbook_clause, Mapping):
        return _text(playbook_clause.get(field))
    return ""


# Generic fix strings the AI-first path emits when it has nothing specific; not
# worth surfacing as a "suggested fix" detail line in the explanation.
_GENERIC_FIX_TEXT = {
    "no change needed.",
    "review the proposed redline.",
    "confirm the clause position before sending.",
}


def _strip_period(value: str) -> str:
    return value.strip().rstrip(".").strip()


def _ensure_period(value: str) -> str:
    text = value.strip()
    if text and not text.endswith((".", "!", "?")):
        return text + "."
    return text


def _lower_first(value: str) -> str:
    if not value:
        return value
    return value[0].lower() + value[1:]


def _text(value: object) -> str:
    return str(value or "").strip()


# Typing alias kept loose: clause results are plain mutable dicts at the call
# sites, but we only read via the Mapping protocol and assign one key.
MutableClause = dict
