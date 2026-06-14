"""Shared, context-aware heading/clause-number detection.

This module is the single source of truth for two questions that used to be
answered independently (and therefore divergently) by
``review_document.align_document_paragraphs`` and
``contract_structure.build_contract_structure``:

1. What clause number does a paragraph block carry?
2. Is a soft-return continuation piece a *new* heading, or just wrapped body
   text of the block it was split from?

The hard case is telling a CONTINUATION ("5 Business Days notice ...") apart
from a REAL heading ("10.1 Return of Materials"). As pure text they are
indistinguishable -- both are ``number + space + Capitalized Words``. The only
reliable signal is structural CONTEXT: a continuation is a soft-return split of
the *same* source block that already carries clause number 5, whereas a real
heading is its own source block. A text-only classifier cannot separate them, so
this detector is explicitly context aware: it accepts the split provenance
(whether the line is a continuation and the parent block's clause number) and
refuses to promote a continuation of an already-numbered block.

Because both layers call into this single module, they cannot diverge: the
second, independent re-derivation of clause numbers that previously undid the
upstream fix no longer exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Identifier grammar -- kept structurally identical to the patterns historically
# used by ``contract_structure`` so the unified detector accepts exactly the same
# clause-number shapes (decimals, letter suffixes, romans, parentheticals).
ROMAN_NUMBER_PATTERN = r"[IVXLCDM]+"
BASE_IDENTIFIER_PART_PATTERN = rf"(?:{ROMAN_NUMBER_PATTERN}|[A-Za-z]|\d+[A-Za-z]*)"
PARENTHETICAL_IDENTIFIER_PART_PATTERN = r"\([A-Za-z0-9]+\)"
IDENTIFIER_PART_PATTERN = (
    rf"(?:{BASE_IDENTIFIER_PART_PATTERN}(?:{PARENTHETICAL_IDENTIFIER_PART_PATTERN})*|"
    rf"{PARENTHETICAL_IDENTIFIER_PART_PATTERN})"
)
NUMBERED_NUMBER_PATTERN = rf"{IDENTIFIER_PART_PATTERN}(?:\.{IDENTIFIER_PART_PATTERN})*"

# A leading clause-number marker: a number identifier followed either by a
# deliberate punctuation separator (``.``/``:``/dash) or by whitespace.
LEADING_NUMBER_RE = re.compile(
    rf"^\s*(?P<number>{NUMBERED_NUMBER_PATTERN})(?P<separator>\s*[:.\-–—]\s*|\s+)(?P<rest>.*)$",
    re.DOTALL,
)
_TRAILING_NUMBER_DOT_RE = re.compile(r"\.$")
_EXPLICIT_SEPARATOR_RE = re.compile(r"[:.\-–—]")


@dataclass(frozen=True)
class LeadingNumber:
    """A parsed leading clause-number marker on a line of text."""

    number: str
    separator: str
    rest: str

    @property
    def has_explicit_separator(self) -> bool:
        """True when the marker used deliberate punctuation (``5.`` / ``5:`` /
        ``5 -``), not bare whitespace (``5 years``). The explicit separator is
        the deliberate clause marker; bare whitespace is body prose."""
        return bool(_EXPLICIT_SEPARATOR_RE.search(self.separator or ""))


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_leading_number(text: str) -> LeadingNumber | None:
    """Parse a leading clause-number marker from ``text`` if present.

    Returns the normalized number (trailing ``.`` stripped, e.g. ``5.`` -> ``5``),
    the raw separator, and the remaining text. Returns ``None`` when the line does
    not begin with a number identifier.
    """
    match = LEADING_NUMBER_RE.match(_collapse_whitespace(text))
    if not match:
        return None
    number = _TRAILING_NUMBER_DOT_RE.sub("", match.group("number").strip())
    if not number:
        return None
    return LeadingNumber(number=number, separator=match.group("separator"), rest=match.group("rest"))


def _normalize_number(number: str | None) -> str:
    if not number:
        return ""
    return _TRAILING_NUMBER_DOT_RE.sub("", str(number).strip()).strip().lower()


def block_clause_number(text: str, structure_number: str | None) -> str:
    """The effective clause number a block carries.

    A block's own text-literal numbered prefix (``2. Second.``) wins over a
    mismatched Word-autonumber ``structure_number`` (e.g. ``1``): the literal the
    author typed into the run is the heading number a reader sees. When the text
    carries no literal prefix the metadata ``structure_number`` is used. This is
    the single definition both ``align_document_paragraphs`` (to compute split
    provenance) and ``contract_structure`` (to label the section) rely on, so the
    two layers agree by construction.
    """
    leading = parse_leading_number(text)
    if leading is not None and leading.has_explicit_separator:
        return leading.number
    metadata_number = _TRAILING_NUMBER_DOT_RE.sub("", str(structure_number or "").strip()).strip()
    if metadata_number:
        return metadata_number
    if leading is not None:
        return leading.number
    return ""


def continuation_is_heading(text: str, parent_number: str | None) -> bool:
    """Decide whether a soft-return CONTINUATION piece is a *new* heading.

    Context-aware contract (the crux of the fix):

    * A continuation whose leading number equals the parent block's clause
      number is wrapped body text of that same clause -- ``5. Confidentiality.``
      / ``5 Business Days notice ...`` -- and is NEVER a heading, regardless of
      the capitalization of the words that follow (the capitalized-continuation
      case that defeated the round-2 text-only guard).
    * A continuation with no leading number, or a leading number separated only
      by whitespace (``5 years following ...``), is body prose -- not a heading.
    * A continuation is promoted to a heading ONLY when it carries its own
      explicit-separator numbered marker whose number DIFFERS from the parent
      (``1`` parent, ``2. Second.`` / ``3. Third.`` pieces): genuinely new
      clauses that happened to share one Word paragraph via soft returns.
    """
    leading = parse_leading_number(text)
    if leading is None:
        return False
    if not leading.has_explicit_separator:
        return False
    if _normalize_number(leading.number) == _normalize_number(parent_number):
        return False
    return True
