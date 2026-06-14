"""Deterministic clause-localization hints (roadmap item #5).

Given the playbook clauses and a document's :func:`contract_structure`, derive a
light per-clause "Locate" hint: which printed section(s) have a heading that looks
like this clause. The hints are surfaced in the AI assessment packet so the model
starts its Locate step in the right place.

This is intentionally MARGINAL: once item #4 tags every paragraph with its section,
the model already sees structure everywhere, so localization is just a nudge. It is
strictly additive and conservative:

* It never asserts a clause is present or absent -- it only suggests where to look.
* It never constrains the model: the packet note tells the model to verify and to
  search the whole document, so a wrong/missing hint cannot move a verdict.
* A clause with no confident heading match simply gets no hint.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Per-clause heading keyword cues. A section whose normalized heading contains any
# of a clause's cue phrases is suggested for that clause. Cues are deliberately
# high-precision (the words that actually appear in a real NDA section heading for
# that topic), not an exhaustive semantic model -- a miss just yields no hint, which
# is the safe default.
_CLAUSE_HEADING_CUES: dict[str, tuple[str, ...]] = {
    "mutuality": ("mutual", "mutuality", "reciprocal"),
    "confidential_information": (
        "confidential information",
        "confidentiality",
        "definition of confidential",
        "proprietary information",
    ),
    "governing_law": (
        "governing law",
        "governing-law",
        "applicable law",
        "choice of law",
        "jurisdiction",
        "law and jurisdiction",
    ),
    "term_and_survival": (
        "term",
        "survival",
        "duration",
        "termination",
        "survive",
    ),
    "non_circumvention": (
        "non-circumvention",
        "non circumvention",
        "noncircumvention",
        "circumvention",
        "non-solicit",
        "non solicitation",
        "exclusivity",
        "no circumvent",
    ),
    "signatures": (
        "signature",
        "signatures",
        "in witness whereof",
        "executed",
        "execution",
    ),
}

_MAX_SUGGESTIONS_PER_CLAUSE = 3


def build_clause_localization(
    playbook: Mapping[str, Any],
    contract_structure: Mapping[str, Any] | None,
) -> dict[str, dict[str, list[str]]]:
    """Map clause_id -> {"suggested_section_ids", "suggested_section_labels"}.

    Returns an empty dict when there is no usable structure, so the caller can pass
    the result straight through and the packet builder simply attaches nothing.
    """
    if not isinstance(contract_structure, Mapping):
        return {}
    sections = contract_structure.get("sections")
    if not isinstance(sections, Sequence):
        return {}

    section_views: list[dict[str, str]] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("id") or "")
        if not section_id:
            continue
        heading = str(section.get("heading") or "")
        label = str(section.get("label") or "")
        normalized = _normalize(f"{heading} {label}")
        if not normalized:
            continue
        section_views.append({
            "id": section_id,
            "label": label or heading,
            "normalized": normalized,
        })
    if not section_views:
        return {}

    clauses = playbook.get("clauses") if isinstance(playbook, Mapping) else None
    if not isinstance(clauses, Sequence):
        return {}

    localization: dict[str, dict[str, list[str]]] = {}
    for clause in clauses:
        if not isinstance(clause, Mapping):
            continue
        clause_id = str(clause.get("id") or "")
        if not clause_id:
            continue
        cues = _cues_for_clause(clause_id, clause)
        if not cues:
            continue
        matched_ids: list[str] = []
        matched_labels: list[str] = []
        for view in section_views:
            if any(cue in view["normalized"] for cue in cues):
                if view["id"] not in matched_ids:
                    matched_ids.append(view["id"])
                    matched_labels.append(view["label"])
            if len(matched_ids) >= _MAX_SUGGESTIONS_PER_CLAUSE:
                break
        if matched_ids:
            localization[clause_id] = {
                "suggested_section_ids": matched_ids,
                "suggested_section_labels": matched_labels,
            }
    return localization


def _cues_for_clause(clause_id: str, clause: Mapping[str, Any]) -> list[str]:
    cues = list(_CLAUSE_HEADING_CUES.get(clause_id, ()))
    # Also fold in the clause's own name (e.g. a custom dynamic clause type whose id is
    # not in the static cue map still gets matched by its display name).
    name = _normalize(str(clause.get("name") or ""))
    if name and name not in cues:
        cues.append(name)
    return [cue for cue in (_normalize(cue) for cue in cues) if cue]


def _normalize(text: str) -> str:
    lowered = str(text or "").lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", collapsed).strip()
