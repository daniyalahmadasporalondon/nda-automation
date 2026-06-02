from __future__ import annotations

import re
from typing import Dict, List

from .common import (
    ClauseResult,
    Paragraph,
    _approved_laws,
    _check,
    _governing_anchor_patterns,
    _governing_law_phrase,
    _governing_law_change_fix,
    _governing_law_missing_fix,
    _literal_word_pattern,
    _match,
    _not_present,
    _paragraph_matches,
)
from .context import attach_structure_context, merge_paragraphs, paragraphs_with_concepts

GOVERNING_LAW_VALUE_PATTERNS = (
    r"\bgoverned\b.{0,120}?\blaws?\s+of\s+(?P<law>[^.;,\n]+)",
    r"\bgoverned\b.{0,120}?\b(?P<law>[^.;,\n]+?)\s+laws?\b",
    r"\bconstrued\b.{0,120}?\blaws?\s+of\s+(?P<law>[^.;,\n]+)",
    r"\bconstrued\b.{0,120}?\b(?P<law>[^.;,\n]+?)\s+laws?\b",
    r"\bsubject\s+to\b.{0,120}?\blaws?\s+of\s+(?P<law>[^.;,\n]+)",
    r"\bsubject\s+to\b.{0,120}?\b(?P<law>[^.;,\n]+?)\s+laws?\b",
    r"\bgoverning\s+law\b.{0,80}?(?:is|shall\s+be|will\s+be|:)\s*(?:the\s+)?(?:laws?\s+of\s+)?(?P<law>[^.;,\n]+)",
)

GOVERNING_LAW_INPUT_ALIASES = {
    "england and wales": ("english",),
    "india": ("indian",),
    "difc": ("dubai international financial centre", "dubai international financial center"),
}


def _check_governing_law(
    _text: str,
    normalized: str,
    clause: Dict[str, object],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object] | None = None,
) -> ClauseResult:
    context_concepts = ["governing_law"]
    governing_anchor_patterns = _governing_anchor_patterns(clause)
    governing_paragraphs = merge_paragraphs(
        _paragraph_matches(paragraphs, governing_anchor_patterns),
        paragraphs_with_concepts(paragraphs, review_context, context_concepts),
    )
    approved_governing_paragraphs = [
        paragraph
        for paragraph in governing_paragraphs
        if _uses_approved_governing_law(str(paragraph["text"]), clause)
    ]

    if approved_governing_paragraphs:
        return attach_structure_context(
            _match(clause, "Approved governing law found.", approved_governing_paragraphs),
            review_context,
            context_concepts,
        )
    if governing_paragraphs:
        return attach_structure_context(_check(
            clause,
            "A governing law clause was found, but it does not use an approved law.",
            governing_paragraphs,
            what_to_fix=_governing_law_change_fix(clause),
        ), review_context, context_concepts)
    return attach_structure_context(_not_present(
        clause,
        "No governing law clause was found.",
        [],
        what_to_fix=_governing_law_missing_fix(clause),
    ), review_context, context_concepts)


def _uses_approved_governing_law(text: str, clause: Dict[str, object]) -> bool:
    candidates = _governing_law_candidates(text)
    if candidates:
        return any(_starts_with_approved_law(candidate, clause) for candidate in candidates)
    return _contains_approved_governing_phrase(text, clause)


def _governing_law_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    for pattern in GOVERNING_LAW_VALUE_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidates.append(match.group("law").strip())
    return candidates


def _starts_with_approved_law(text: str, clause: Dict[str, object]) -> bool:
    candidate = _trim_governing_law_candidate(text)
    for law in _approved_laws(clause):
        for term in _approved_law_input_terms(clause, law):
            if re.search(rf"^\s*(?:the\s+)?{_literal_word_pattern(term)}", candidate, flags=re.IGNORECASE):
                return True
    return False


def _trim_governing_law_candidate(text: str) -> str:
    return re.sub(
        r"^\s*(?:by|under|in\s+accordance\s+with|according\s+to|pursuant\s+to)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _contains_approved_governing_phrase(text: str, clause: Dict[str, object]) -> bool:
    for law in _approved_laws(clause):
        for term in _approved_law_input_terms(clause, law):
            if re.search(rf"\blaws?\s+of\s+{_literal_word_pattern(term)}", text, flags=re.IGNORECASE):
                return True
            if re.search(rf"{_literal_word_pattern(term)}\s+laws?\b", text, flags=re.IGNORECASE):
                return True
    return False


def _approved_law_input_terms(clause: Dict[str, object], law: str) -> List[str]:
    terms = [law, _governing_law_phrase(clause, law)]
    terms.extend(GOVERNING_LAW_INPUT_ALIASES.get(law.lower().strip(), ()))
    return list(dict.fromkeys(term for term in terms if term))
