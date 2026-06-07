"""Canonical redline wording, sourced from the Playbook.

The AI-first review pipeline asks the model for clause-level *judgment*
(pass / fail / review) but NOT for the corrected wording: the Playbook already
stores the standard replacement sentence per clause. This module derives that
wording from a Playbook clause so the contract validator can default a blank
``proposed_redline.text`` instead of rejecting the whole document.

It deliberately mirrors how the deterministic engine and the generator resolve
the same fix (``checker._governing_law_replacement_text``,
``checker._term_and_survival_replacement_text``,
``checks.common._clause_template_text``) so all three paths produce identical
corrected wording. It depends only on ``checks.common`` helpers to stay free of
import cycles with the contract module.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .checks.common import (
    PlaybookTemplateError,
    _approved_laws,
    _clause_template_text,
    _governing_law_phrase,
    _max_term_years,
    _year_count_label,
)

__all__ = ["playbook_redline_text"]


def playbook_redline_text(clause: Mapping[str, Any] | None) -> str:
    """Return the Playbook's canonical replacement wording for ``clause``.

    Returns an empty string when the clause carries no usable template (e.g. a
    prohibited clause whose fix is a deletion, or a governing-law clause with no
    approved laws). Callers treat the empty result as "no template available"
    and degrade that single clause gracefully rather than failing the document.
    """
    if not isinstance(clause, Mapping):
        return ""

    clause_id = str(clause.get("id") or "").strip()

    # governing_law has no redline_template; the approved jurisdiction IS the fix.
    if clause_id == "governing_law" or not str(clause.get("redline_template") or "").strip():
        if clause_id == "governing_law":
            return _governing_law_default_text(clause)
        # A non-governing-law clause with no redline_template (e.g. a prohibited
        # clause whose fix is deletion) has no replacement wording to default.
        return ""

    # term_and_survival's template carries a {max_term_years_label} placeholder.
    context: dict[str, Any] = {}
    if clause_id == "term_and_survival" or "{max_term_years" in str(clause.get("redline_template") or ""):
        max_term_years = _max_term_years(clause)
        context = {
            "max_term_years": max_term_years,
            "max_term_years_label": _year_count_label(max_term_years),
        }

    try:
        return _clause_template_text(clause, "redline_template", context)
    except PlaybookTemplateError:
        # A malformed template must never sink the whole document; the caller
        # degrades this one clause to a no-text flag instead.
        return ""


def _governing_law_default_text(clause: Mapping[str, Any]) -> str:
    """Mirror ``checker._governing_law_replacement_text`` for the preferred law.

    Picks the Playbook's preferred approved law (falling back to the first
    approved law) and renders the same operative sentence the deterministic
    governing-law redline emits.
    """
    law = _preferred_governing_law(clause)
    if not law:
        return ""
    law_phrase = _governing_law_phrase(dict(clause), law)
    return f"This Agreement shall be governed by the laws of {law_phrase}."


def _preferred_governing_law(clause: Mapping[str, Any]) -> str:
    approved_laws = _approved_laws(dict(clause))
    preferred_law = str(clause.get("preferred_law") or "").strip()
    if preferred_law and (not approved_laws or preferred_law in approved_laws):
        return preferred_law
    if approved_laws:
        return approved_laws[0]
    return ""
