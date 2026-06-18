"""The canonical GOVERNING-LAW -> COURT/FORUM pairing, sourced from the Playbook.

This module is the single source of truth for "which court goes with which
approved governing law". Both the generation side (which writes the
forum/submission clause into a drafted NDA) and the review side (which detects a
mismatched law/forum pairing in a counterparty's document) read the pairing FROM
the Playbook's ``governing_law`` approved options here, so neither carries a
hardcoded duplicate that can silently diverge from ``playbook.json``.

The Playbook's ``governing_law.rules.approved_options`` carry, per option:

    {
      "id": "england_and_wales",
      "label": "England and Wales",
      "value": "England and Wales",
      "court_name": "the courts of England and Wales",
      "forum_jurisdiction": "England and Wales",
      ...
    }

``court_name`` is the proper COURT/VENUE string to write into the submission
clause (a court, not a bare jurisdiction). ``forum_jurisdiction`` is the
jurisdiction whose courts have authority (the descriptor the review-side detector
pairs against the law). Both are read straight off the matching approved option.

Public API (a parallel review-side build depends on this -- the signature is
stable):

* ``canonical_forum_for_law(playbook, law_option_id) -> dict | None`` -- the
  canonical pairing for an approved option id, or ``None`` for an unknown id.
* ``court_name_for_law(playbook, law_option_id) -> str`` -- just the court string
  (``""`` when unknown), the value the generation forum gate consumes.

Every function is PURE and defensive: it reads only the passed-in ``playbook``
mapping, never the filesystem or any module-global state, so callers can pass a
test playbook to prove the pairing FOLLOWS the Playbook rather than a hardcode.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "canonical_forum_for_law",
    "court_name_for_law",
    "approved_governing_law_options",
]

_GOVERNING_LAW_CLAUSE_ID = "governing_law"


def canonical_forum_for_law(playbook: dict, law_option_id: str) -> dict | None:
    """The canonical court/forum pairing for an approved governing-law option.

    Reads the matching ``governing_law`` approved option straight from the passed
    ``playbook`` and returns::

        {
            "option_id": str,            # the approved option's id
            "law_label": str,            # its human label (value/label)
            "forum_jurisdiction": str,   # the jurisdiction whose courts apply
            "court_name": str,           # the proper COURT/VENUE string to write
        }

    Returns ``None`` when ``law_option_id`` matches no approved option, so a
    caller can treat an unknown option as "no canonical forum" rather than
    fabricating one. Pure: reads only ``playbook``; never touches the filesystem
    or any module state.
    """

    target = str(law_option_id or "").strip().lower()
    if not target:
        return None
    for option in approved_governing_law_options(playbook):
        option_id = str(option.get("id") or "").strip()
        if option_id.lower() != target:
            continue
        law_label = str(option.get("value") or option.get("label") or option_id).strip()
        return {
            "option_id": option_id,
            "law_label": law_label,
            "forum_jurisdiction": str(option.get("forum_jurisdiction") or "").strip(),
            "court_name": str(option.get("court_name") or "").strip(),
        }
    return None


def court_name_for_law(playbook: dict, law_option_id: str) -> str:
    """The proper court/venue string for an approved option, or ``""`` if unknown.

    Thin accessor over :func:`canonical_forum_for_law` for the generation forum
    gate, which only needs the court string. Returns ``""`` for an unknown option
    id OR an approved option that carries no ``court_name`` -- the generation gate
    turns an empty court into a hard refusal rather than writing a non-court venue.
    """

    pairing = canonical_forum_for_law(playbook, law_option_id)
    if pairing is None:
        return ""
    return pairing["court_name"]


def approved_governing_law_options(playbook: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The Playbook ``governing_law`` approved options (defensive, pure).

    Reads ``clauses[governing_law].rules.approved_options`` from the passed
    mapping. Any unexpected shape yields an empty list rather than raising, so the
    pairing helpers degrade to "no canonical forum" instead of breaking a caller.
    """

    if not isinstance(playbook, Mapping):
        return []
    clauses = playbook.get("clauses")
    if not isinstance(clauses, (list, tuple)):
        return []
    for clause in clauses:
        if not isinstance(clause, Mapping):
            continue
        if str(clause.get("id") or "").strip() != _GOVERNING_LAW_CLAUSE_ID:
            continue
        rules = clause.get("rules")
        options = rules.get("approved_options") if isinstance(rules, Mapping) else None
        if not isinstance(options, (list, tuple)):
            return []
        return [option for option in options if isinstance(option, Mapping)]
    return []
