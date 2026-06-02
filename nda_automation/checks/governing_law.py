from __future__ import annotations

import re
from typing import Dict, Iterable, List

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
APPROVED_GOVERNING_LAW_ENTITY_PREFIXES = {
    "delaware": ("state", "commonwealth"),
    "india": ("republic",),
}
UNCLEAR_GOVERNING_LAW_CANDIDATE_PATTERN = (
    r"(?:\[[^\]]*\]|_{2,}|\btbd\b|\bto\s+be\s+(?:agreed|determined|selected|inserted)\b|"
    r"\b(?:mutually\s+)?agreed\b|\b(?:applicable|relevant)\s+law\b|\bjurisdiction\b|"
    r"\b(?:chosen|selected|determined)\s+by\b|\b(?:disclosing|receiving)\s+party\b|"
    r"\bprincipal\s+place\b|\bstate\s+where\b|\bcountry\s+where\b)"
)
APPROVED_GOVERNING_LAW_REVIEW_PATTERN = (
    r"\b(?:or|unless|as\s+otherwise|otherwise\s+agreed|chosen\s+by|selected\s+by|"
    r"determined\s+by|to\s+be\s+(?:agreed|determined|selected))\b"
)
SECONDARY_GOVERNING_LAW_FRAGMENT_PATTERN = (
    r"\b(?:except(?:\s+that)?|provided(?:\s+that)?|save(?:\s+that)?|notwithstanding|however)\b"
    r"(?=[^.;\n]{0,180}\b(?:governed|construed|subject\s+to|laws?\s+of|law\s+of)\b)"
    r"[^.;\n]+"
)


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
    paragraph_analysis = _governing_law_paragraph_analysis(governing_paragraphs, clause)
    approved_governing_paragraphs = paragraph_analysis["approved_paragraphs"]
    unclear_governing_paragraphs = paragraph_analysis["unclear_paragraphs"]
    unapproved_governing_paragraphs = paragraph_analysis["unapproved_paragraphs"]
    heading_only_paragraphs = paragraph_analysis["heading_only_paragraphs"]
    analysis = _governing_law_analysis(paragraph_analysis)

    if approved_governing_paragraphs and not unclear_governing_paragraphs and not unapproved_governing_paragraphs:
        result = _match(clause, "Approved governing law found.", approved_governing_paragraphs)
        _attach_governing_law_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if approved_governing_paragraphs and (unclear_governing_paragraphs or unapproved_governing_paragraphs):
        result = _review(
            clause,
            (
                "An approved governing law was found, but the document also contains unclear, "
                "conditional, or non-approved governing-law language."
            ),
            approved_governing_paragraphs + unclear_governing_paragraphs + unapproved_governing_paragraphs,
            what_to_verify=(
                "Confirm which governing law controls and remove any conflicting or conditional "
                "governing-law language."
            ),
        )
        _attach_governing_law_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if unclear_governing_paragraphs:
        result = _review(
            clause,
            "A governing law clause was found, but the governing jurisdiction is unclear or unresolved.",
            unclear_governing_paragraphs,
            what_to_verify=(
                "Confirm the intended governing jurisdiction and replace placeholder, conditional, "
                "or unresolved governing-law language with an approved law."
            ),
        )
        _attach_governing_law_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if unapproved_governing_paragraphs:
        result = _check(
            clause,
            "A governing law clause was found, but it does not use an approved law.",
            unapproved_governing_paragraphs,
            what_to_fix=_governing_law_change_fix(clause),
        )
        _attach_governing_law_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    if heading_only_paragraphs:
        result = _review(
            clause,
            "A governing law heading was found, but no governing jurisdiction was stated.",
            heading_only_paragraphs,
            what_to_verify=(
                "Confirm the intended governing jurisdiction and add an approved governing-law sentence."
            ),
        )
        _attach_governing_law_analysis(result, analysis)
        return attach_structure_context(result, review_context, context_concepts)

    result = _not_present(
        clause,
        "No governing law clause was found.",
        [],
        what_to_fix=_governing_law_missing_fix(clause),
    )
    _attach_governing_law_analysis(result, analysis)
    return attach_structure_context(result, review_context, context_concepts)


def _review(
    clause: Dict[str, object],
    reason: str,
    matched_paragraphs: Iterable[Paragraph],
    *,
    what_to_verify: str,
) -> ClauseResult:
    result = _match(clause, reason, matched_paragraphs)
    result["decision"] = "review"
    result["needs_review"] = True
    result["review_reason"] = reason
    result["decision_reason"] = reason
    result["what_to_fix"] = what_to_verify
    return result


def _governing_law_paragraph_analysis(
    governing_paragraphs: Iterable[Paragraph],
    clause: Dict[str, object],
) -> Dict[str, object]:
    approved_paragraphs: List[Paragraph] = []
    unclear_paragraphs: List[Paragraph] = []
    unapproved_paragraphs: List[Paragraph] = []
    heading_only_paragraphs: List[Paragraph] = []
    candidate_records: List[Dict[str, object]] = []

    for paragraph in governing_paragraphs:
        text = str(paragraph["text"])
        candidates = _governing_law_candidates(text)
        records = [
            _governing_law_candidate_record(str(paragraph.get("id") or ""), candidate, clause)
            for candidate in candidates
        ]
        candidate_records.extend(records)

        approved_records = [record for record in records if record["approved"] and not record["needs_review"]]
        unclear_records = [record for record in records if record["needs_review"]]
        unapproved_records = [record for record in records if not record["approved"] and not record["needs_review"]]

        if approved_records and not unclear_records and not unapproved_records:
            approved_paragraphs.append(paragraph)
        elif approved_records or unclear_records:
            unclear_paragraphs.append(paragraph)
        elif not records and _contains_approved_governing_phrase(text, clause):
            if _has_unclear_governing_law_text(text):
                unclear_paragraphs.append(paragraph)
            else:
                approved_paragraphs.append(paragraph)
        elif not records and _is_governing_law_heading_only(text):
            heading_only_paragraphs.append(paragraph)
        else:
            unapproved_paragraphs.append(paragraph)

    return {
        "approved_paragraphs": approved_paragraphs,
        "unclear_paragraphs": unclear_paragraphs,
        "unapproved_paragraphs": unapproved_paragraphs,
        "heading_only_paragraphs": heading_only_paragraphs,
        "candidate_records": candidate_records,
    }


def _governing_law_candidate_record(
    paragraph_id: str,
    candidate: str,
    clause: Dict[str, object],
) -> Dict[str, object]:
    approved = _starts_with_approved_law(candidate, clause)
    needs_review = _is_unclear_governing_law_candidate(candidate) or (
        approved and _approved_governing_candidate_needs_review(candidate)
    )
    return {
        "paragraph_id": paragraph_id,
        "value": _trim_governing_law_candidate(candidate),
        "approved": approved,
        "needs_review": needs_review,
    }


def _uses_approved_governing_law(text: str, clause: Dict[str, object]) -> bool:
    candidates = _governing_law_candidates(text)
    if candidates:
        return any(_starts_with_approved_law(candidate, clause) for candidate in candidates)
    return _contains_approved_governing_phrase(text, clause)


def _governing_law_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    seen = set()
    for fragment in _governing_law_candidate_fragments(text):
        for pattern in GOVERNING_LAW_VALUE_PATTERNS:
            for match in re.finditer(pattern, fragment, flags=re.IGNORECASE):
                candidate = match.group("law").strip()
                candidate_key = _trim_governing_law_candidate(candidate).lower()
                if _is_noise_governing_law_candidate(candidate) or candidate_key in seen:
                    continue
                candidates.append(candidate)
                seen.add(candidate_key)
    return candidates


def _governing_law_candidate_fragments(text: str) -> List[str]:
    fragments = [text]
    seen = {text}
    for match in re.finditer(SECONDARY_GOVERNING_LAW_FRAGMENT_PATTERN, text, flags=re.IGNORECASE):
        fragment = match.group(0).strip(" ,")
        if not fragment or fragment in seen:
            continue
        fragments.append(fragment)
        seen.add(fragment)
    return fragments


def _starts_with_approved_law(text: str, clause: Dict[str, object]) -> bool:
    candidate = _trim_governing_law_candidate(text)
    for law in _approved_laws(clause):
        entity_prefix_pattern = _approved_governing_law_entity_prefix_pattern(law)
        for term in _approved_law_input_terms(clause, law):
            if re.search(
                rf"^\s*(?:the\s+)?{entity_prefix_pattern}"
                rf"{_literal_word_pattern(term)}",
                candidate,
                flags=re.IGNORECASE,
            ):
                return True
    return False


def _trim_governing_law_candidate(text: str) -> str:
    return re.sub(
        r"^\s*(?:(?:by|under|in\s+accordance\s+with|according\s+to|pursuant\s+to)(?:\s+the)?(?:\s+|$)|the(?:\s+|$))",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _is_noise_governing_law_candidate(candidate: str) -> bool:
    trimmed = _trim_governing_law_candidate(candidate).lower()
    return trimmed in {"", "law", "laws"} or bool(
        re.search(r"\b(?:by|under|with|according\s+to|pursuant\s+to)\s+(?:the(?:\s+|$))?$", trimmed)
    )


def _is_unclear_governing_law_candidate(candidate: str) -> bool:
    trimmed = _trim_governing_law_candidate(candidate)
    return bool(re.search(UNCLEAR_GOVERNING_LAW_CANDIDATE_PATTERN, trimmed, flags=re.IGNORECASE))


def _approved_governing_candidate_needs_review(candidate: str) -> bool:
    trimmed = _trim_governing_law_candidate(candidate)
    return bool(re.search(APPROVED_GOVERNING_LAW_REVIEW_PATTERN, trimmed, flags=re.IGNORECASE))


def _has_unclear_governing_law_text(text: str) -> bool:
    return bool(re.search(UNCLEAR_GOVERNING_LAW_CANDIDATE_PATTERN, text, flags=re.IGNORECASE))


def _is_governing_law_heading_only(text: str) -> bool:
    return bool(
        re.match(
            r"^\s*(?:(?:article|clause|section)\s+[A-Za-z0-9IVXLCivxlc.() -]+\s*:?\s*)?"
            r"governing\s+law\s*[:.-]?\s*$",
            text,
            flags=re.IGNORECASE,
        )
    )


def _contains_approved_governing_phrase(text: str, clause: Dict[str, object]) -> bool:
    for law in _approved_laws(clause):
        entity_prefix_pattern = _approved_governing_law_entity_prefix_pattern(law)
        for term in _approved_law_input_terms(clause, law):
            if re.search(
                rf"\blaws?\s+of\s+(?:the\s+)?{entity_prefix_pattern}"
                rf"{_literal_word_pattern(term)}",
                text,
                flags=re.IGNORECASE,
            ):
                return True
            if re.search(rf"{_literal_word_pattern(term)}\s+laws?\b", text, flags=re.IGNORECASE):
                return True
    return False


def _approved_governing_law_entity_prefix_pattern(law: str) -> str:
    prefixes = APPROVED_GOVERNING_LAW_ENTITY_PREFIXES.get(law.lower().strip(), ())
    if not prefixes:
        return ""
    escaped_prefixes = "|".join(re.escape(prefix) for prefix in prefixes)
    return rf"(?:(?:{escaped_prefixes})\s+of\s+)?"


def _approved_law_input_terms(clause: Dict[str, object], law: str) -> List[str]:
    terms = [law, _governing_law_phrase(clause, law)]
    terms.extend(GOVERNING_LAW_INPUT_ALIASES.get(law.lower().strip(), ()))
    return list(dict.fromkeys(term for term in terms if term))


def _governing_law_analysis(paragraph_analysis: Dict[str, object]) -> Dict[str, object]:
    approved_paragraphs = paragraph_analysis.get("approved_paragraphs", [])
    unclear_paragraphs = paragraph_analysis.get("unclear_paragraphs", [])
    unapproved_paragraphs = paragraph_analysis.get("unapproved_paragraphs", [])
    heading_only_paragraphs = paragraph_analysis.get("heading_only_paragraphs", [])
    candidate_records = paragraph_analysis.get("candidate_records", [])
    return {
        "approved_paragraph_ids": _paragraph_ids(approved_paragraphs if isinstance(approved_paragraphs, list) else []),
        "unclear_paragraph_ids": _paragraph_ids(unclear_paragraphs if isinstance(unclear_paragraphs, list) else []),
        "unapproved_paragraph_ids": _paragraph_ids(unapproved_paragraphs if isinstance(unapproved_paragraphs, list) else []),
        "heading_only_paragraph_ids": _paragraph_ids(
            heading_only_paragraphs if isinstance(heading_only_paragraphs, list) else []
        ),
        "candidate_records": candidate_records if isinstance(candidate_records, list) else [],
    }


def _attach_governing_law_analysis(result: ClauseResult, analysis: Dict[str, object]) -> None:
    result["governing_law_analysis"] = analysis


def _paragraph_ids(paragraphs: Iterable[Paragraph]) -> List[str]:
    return [str(paragraph.get("id") or "") for paragraph in paragraphs if paragraph.get("id")]
