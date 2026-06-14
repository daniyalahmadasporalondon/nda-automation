from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

from .contract_structure import IDENTIFIER_PART_PATTERN
from .review_document import Paragraph

REFERENCE_RESOLVER_VERSION = 1
REFERENCE_INTEGRITY_VERSION = 1

# A parse has "collapsed" when the structure detector failed to find real section
# boundaries, leaving one section that owns (nearly) the whole document. Cross-
# reference targets cannot be trusted against such a map -- every "Schedule 3"
# would look dangling because the schedule heading was never split out -- so the
# integrity signal disables itself rather than crying wolf. A single section, or
# one section owning more than this share of the mapped paragraphs, trips it.
COLLAPSE_DOMINANT_SECTION_RATIO = 0.70
REFERENCE_KIND_PATTERN = (
    r"clause|clauses|article|articles|section|sections|schedule|schedules|"
    r"annex|annexes|annexure|annexures|appendix|appendices"
)
REFERENCE_NUMBER_PATTERN = rf"{IDENTIFIER_PART_PATTERN}(?:\.{IDENTIFIER_PART_PATTERN})*"
REFERENCE_RANGE_SEPARATOR_PATTERN = r"(?:\s+(?:to|through)\s+|\s*[-\u2013\u2014]\s*)"
REFERENCE_NUMERIC_RANGE_PATTERN = rf"\d+{REFERENCE_RANGE_SEPARATOR_PATTERN}\d+"
REFERENCE_NUMBER_OR_RANGE_PATTERN = rf"(?:{REFERENCE_NUMERIC_RANGE_PATTERN}|{REFERENCE_NUMBER_PATTERN})"
REFERENCE_SEPARATOR_PATTERN = r"(?:\s*(?:,|;)\s*(?:(?:and|or)\s+)?|\s+(?:and|or|&)\s+)"
REFERENCE_EXPRESSION_RE = re.compile(
    rf"\b(?P<kind>{REFERENCE_KIND_PATTERN})\s+"
    rf"(?P<numbers>{REFERENCE_NUMBER_OR_RANGE_PATTERN}(?:{REFERENCE_SEPARATOR_PATTERN}{REFERENCE_NUMBER_OR_RANGE_PATTERN})*)"
    r"(?=$|[^A-Za-z0-9])",
    re.IGNORECASE,
)
REFERENCE_NUMBER_RE = re.compile(REFERENCE_NUMBER_PATTERN, re.IGNORECASE)
REFERENCE_NUMERIC_RANGE_RE = re.compile(
    rf"^(?P<start>\d+){REFERENCE_RANGE_SEPARATOR_PATTERN}(?P<end>\d+)$",
    re.IGNORECASE,
)
REFERENCE_SEPARATOR_RE = re.compile(REFERENCE_SEPARATOR_PATTERN, re.IGNORECASE)
MAX_REFERENCE_RANGE_SIZE = 50

REFERENCE_KIND_ALIASES = {
    "annex": "annex",
    "annexes": "annex",
    "annexure": "annexure",
    "annexures": "annexure",
    "appendices": "appendix",
    "appendix": "appendix",
    "article": "article",
    "articles": "article",
    "clause": "clause",
    "clauses": "clause",
    "schedule": "schedule",
    "schedules": "schedule",
    "section": "section",
    "sections": "section",
}

# "Schedule 2" and "Section 2" are different things: schedules/annexes/appendices
# are attachments numbered in their own space, separate from the in-body
# clauses/articles/sections. The kind-agnostic ``number:N`` alias exists so a
# "Section 10.1" reference can still find a bare numbered heading ("10.1 Return
# of Materials") that carries no explicit kind. But that same fallback must not
# bridge across this divide -- letting "Schedule 2" resolve onto "Section 2" (or
# vice versa) produces a latent false-clear in the governing-law check. We only
# allow the numeric fallback within the same namespace.
REFERENCE_KIND_NAMESPACES = {
    "annex": "attachment",
    "annexure": "attachment",
    "appendix": "attachment",
    "schedule": "attachment",
    "article": "body",
    "clause": "body",
    "section": "body",
}
# Bare numbered/heading sections detected without an explicit kind belong to the
# in-body namespace: they are the clauses/sections a "Section N" reference means.
NUMERIC_FALLBACK_NAMESPACE = "body"


def _kind_namespace(kind: str) -> str | None:
    return REFERENCE_KIND_NAMESPACES.get(str(kind or "").lower())


def resolve_document_references(
    paragraphs: List[Paragraph],
    contract_structure: Dict[str, object],
) -> Dict[str, object]:
    """Resolve explicit clause/article/section references against a structure map."""
    reference_index = contract_structure.get("reference_index")
    if not isinstance(reference_index, dict):
        reference_index = {}
    alias_lookup = _string_dict(reference_index.get("alias_to_section_id"))
    ambiguous_alias_keys = _string_set(reference_index.get("ambiguous_alias_keys"))
    sections_by_id = _section_lookup(reference_index.get("sections_by_id"))
    paragraph_lookup = _string_dict(reference_index.get("paragraph_to_section_id"))

    references: List[Dict[str, object]] = []
    for paragraph in paragraphs:
        references.extend(_references_for_paragraph(
            paragraph,
            alias_lookup=alias_lookup,
            ambiguous_alias_keys=ambiguous_alias_keys,
            sections_by_id=sections_by_id,
            paragraph_lookup=paragraph_lookup,
            start_index=len(references) + 1,
        ))

    resolved_count = sum(1 for reference in references if reference["status"] == "resolved")
    partial_count = sum(1 for reference in references if reference["status"] == "partial")
    unresolved_count = sum(1 for reference in references if reference["status"] == "unresolved")
    target_ids = {
        section_id
        for reference in references
        for section_id in reference.get("resolved_section_ids", [])
        if isinstance(section_id, str)
    }

    return {
        "version": REFERENCE_RESOLVER_VERSION,
        "references": references,
        "stats": {
            "reference_count": len(references),
            "resolved_reference_count": resolved_count,
            "partial_reference_count": partial_count,
            "unresolved_reference_count": unresolved_count,
            "target_section_count": len(target_ids),
        },
    }


def build_reference_integrity_signal(
    reference_resolver: Dict[str, object] | None,
    contract_structure: Dict[str, object] | None,
) -> Dict[str, object]:
    """Aggregate dangling/ambiguous cross-references into a document-level signal.

    The reference resolver already computes per-reference resolution status and the
    structure already flags ambiguous alias collisions; both are otherwise discarded.
    This rolls them up into one additive, human-readable signal -- e.g. "Schedule 3
    is referenced but no Schedule 3 section exists".

    Guards (without these it cries wolf on every PDF / collapsed parse):
      1. DOCX-with-numbering only -- ``applicable`` is ``False`` for a PDF / plain
         text parse (``docx_numbered_paragraph_count == 0``); the cross-reference
         map is only trustworthy when the extractor stamped real numbering.
      2. Collapse detector -- if there is a single section, or one section owns more
         than ``COLLAPSE_DOMINANT_SECTION_RATIO`` of the mapped paragraphs, the parse
         collapsed and the signal disables itself.
      3. Ambiguous-alias collisions are reported as UNKNOWN, never a dangling
         violation: a number claimed by two restarted-numbering sections is a
         resolver limitation, not a drafting defect.

    Always returns a dict (never raises); when disabled it carries ``applicable:
    False`` and an empty issue list so the key is stable for consumers.
    """
    references = []
    if isinstance(reference_resolver, dict):
        references = [
            reference
            for reference in reference_resolver.get("references", [])
            if isinstance(reference, dict)
        ]

    skipped_reason = _integrity_skip_reason(contract_structure)
    if skipped_reason:
        return {
            "version": REFERENCE_INTEGRITY_VERSION,
            "applicable": False,
            "skipped_reason": skipped_reason,
            "status": "ok",
            "dangling_reference_count": 0,
            "ambiguous_reference_count": 0,
            "issues": [],
        }

    dangling_issues: List[Dict[str, object]] = []
    ambiguous_issues: List[Dict[str, object]] = []
    for reference in references:
        if str(reference.get("status") or "") not in {"partial", "unresolved"}:
            continue
        dangling_labels, ambiguous_labels = _classify_unresolved_items(reference)
        reference_text = str(reference.get("reference_text") or "").strip()
        source_section_id = reference.get("source_section_id")
        if dangling_labels:
            dangling_issues.append({
                "reference_text": reference_text,
                "kind": str(reference.get("kind") or ""),
                "missing_numbers": dangling_labels,
                "source_section_id": source_section_id if isinstance(source_section_id, str) else None,
                "summary": _integrity_summary(reference_text, dangling_labels),
            })
        if ambiguous_labels:
            ambiguous_issues.append({
                "reference_text": reference_text,
                "kind": str(reference.get("kind") or ""),
                "ambiguous_numbers": ambiguous_labels,
                "source_section_id": source_section_id if isinstance(source_section_id, str) else None,
                "summary": (
                    f"{reference_text or 'A cross-reference'} matches more than one section "
                    "(restarted numbering); its target is unknown."
                ),
            })

    return {
        "version": REFERENCE_INTEGRITY_VERSION,
        "applicable": True,
        "skipped_reason": "",
        "status": "issues_found" if dangling_issues else "ok",
        "dangling_reference_count": len(dangling_issues),
        "ambiguous_reference_count": len(ambiguous_issues),
        "issues": dangling_issues,
        "ambiguous_issues": ambiguous_issues,
    }


def _integrity_skip_reason(contract_structure: Dict[str, object] | None) -> str:
    """Return why the integrity signal is disabled, or "" when it may run.

    Implements guard #1 (DOCX-with-numbering only) and guard #2 (collapse detector).
    """
    if not isinstance(contract_structure, dict):
        return "no_structure"
    stats = contract_structure.get("stats")
    stats = stats if isinstance(stats, dict) else {}
    numbered = stats.get("docx_numbered_paragraph_count")
    if not isinstance(numbered, int) or numbered <= 0:
        # PDF / plain-text parse: no numbering metadata, so cross-reference targets
        # cannot be trusted. (Guard #1.)
        return "not_docx_numbered"

    sections = contract_structure.get("sections")
    sections = sections if isinstance(sections, list) else []
    section_count = stats.get("section_count")
    if not isinstance(section_count, int):
        section_count = len(sections)
    if section_count <= 1:
        return "collapsed_single_section"

    paragraph_counts = [
        len(section.get("paragraph_ids"))
        for section in sections
        if isinstance(section, dict) and isinstance(section.get("paragraph_ids"), list)
    ]
    total = sum(paragraph_counts)
    if total > 0 and max(paragraph_counts, default=0) > COLLAPSE_DOMINANT_SECTION_RATIO * total:
        # One section swallowed the document -- the parse collapsed. (Guard #2.)
        return "collapsed_dominant_section"
    return ""


def _classify_unresolved_items(reference: Dict[str, object]) -> Tuple[List[str], List[str]]:
    """Split a reference's unresolved numbers into genuinely-dangling vs ambiguous.

    An item flagged ``ambiguous`` collided with more than one section (restarted
    numbering); its target is UNKNOWN, never a dangling-reference violation (guard
    #3). Everything else unresolved is a true dangling reference.
    """
    items = reference.get("items")
    dangling: List[str] = []
    ambiguous: List[str] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("section_id"):
                continue
            number = str(item.get("number") or "")
            if not number:
                continue
            if item.get("ambiguous"):
                ambiguous.append(number)
            else:
                dangling.append(number)
        return _dedupe(dangling), _dedupe(ambiguous)

    # Fallback for a reference carrying only ``unresolved_numbers`` (no item detail):
    # treat them as dangling, since ambiguity cannot be distinguished.
    dangling = [
        str(number)
        for number in reference.get("unresolved_numbers", [])
        if str(number)
    ]
    return _dedupe(dangling), ambiguous


def _integrity_summary(reference_text: str, missing_numbers: List[str]) -> str:
    label = reference_text or "A cross-reference"
    if missing_numbers:
        joined = ", ".join(missing_numbers)
        return f"{label} is referenced but no matching section ({joined}) exists in the document."
    return f"{label} is referenced but no matching section exists in the document."


def _references_for_paragraph(
    paragraph: Paragraph,
    *,
    alias_lookup: Dict[str, str],
    ambiguous_alias_keys: set[str],
    sections_by_id: Dict[str, Dict[str, object]],
    paragraph_lookup: Dict[str, str],
    start_index: int,
) -> List[Dict[str, object]]:
    paragraph_text = str(paragraph.get("text") or "")
    paragraph_id = _paragraph_id(paragraph)
    source_section_id = paragraph_lookup.get(paragraph_id or "")
    references: List[Dict[str, object]] = []

    for match in REFERENCE_EXPRESSION_RE.finditer(paragraph_text):
        kind = _canonical_kind(match.group("kind"))
        numbers = _reference_numbers(match.group("numbers"))
        if not kind or not numbers:
            continue
        items = [
            _resolve_reference_item(kind, number, alias_lookup, ambiguous_alias_keys, sections_by_id)
            for number in numbers
        ]
        resolved_section_ids = _dedupe(
            str(item["section_id"])
            for item in items
            if isinstance(item.get("section_id"), str) and item.get("section_id")
        )
        if _is_self_heading_reference(match, source_section_id, resolved_section_ids):
            continue
        unresolved_numbers = [
            str(item["number"])
            for item in items
            if not item.get("section_id")
        ]
        references.append({
            "id": f"reference-{start_index + len(references)}",
            "paragraph_id": paragraph_id,
            "paragraph_index": paragraph.get("index") if isinstance(paragraph.get("index"), int) else None,
            "source_section_id": source_section_id,
            "reference_text": match.group(0),
            "kind": kind,
            "numbers": numbers,
            "items": items,
            "resolved_section_ids": resolved_section_ids,
            "unresolved_numbers": unresolved_numbers,
            "targets": [
                sections_by_id[section_id]
                for section_id in resolved_section_ids
                if section_id in sections_by_id
            ],
            "status": _reference_status(items),
        })

    return references


def _resolve_reference_item(
    kind: str,
    number: str,
    alias_lookup: Dict[str, str],
    ambiguous_alias_keys: set[str],
    sections_by_id: Dict[str, Dict[str, object]],
) -> Dict[str, object]:
    reference_namespace = _kind_namespace(kind)
    alias_keys = [f"{kind}:{number.lower()}"]
    # The kind-agnostic numeric fallback only applies inside the in-body
    # namespace; an attachment reference ("Schedule 2") must match its explicit
    # kind alias and never borrow a Section/numbered heading that happens to
    # share the number.
    if reference_namespace != "attachment":
        alias_keys.append(f"number:{number.lower()}")
    matched_alias = ""
    section_id = ""
    ambiguous = False
    for alias_key in alias_keys:
        # The alias key is claimed by more than one section (e.g. a "Section 2" that
        # recurs in an appended Exhibit with restarted numbering). Binding it to one
        # occurrence would mis-resolve with full confidence; leave it unresolved so
        # downstream consumers force human review instead of trusting the wrong target.
        if alias_key in ambiguous_alias_keys:
            ambiguous = True
            continue
        candidate_section_id = alias_lookup.get(alias_key)
        if not candidate_section_id:
            continue
        if alias_key.startswith("number:") and not _numeric_fallback_namespace_matches(
            reference_namespace, candidate_section_id, sections_by_id
        ):
            continue
        matched_alias = alias_key
        section_id = candidate_section_id
        break
    return {
        "number": number,
        "alias_keys": alias_keys,
        "matched_alias": matched_alias or None,
        "section_id": section_id or None,
        "ambiguous": ambiguous and not section_id,
        "label": str(sections_by_id.get(section_id, {}).get("label") or "") if section_id else "",
        "status": "resolved" if section_id else "unresolved",
    }


def _numeric_fallback_namespace_matches(
    reference_namespace: str | None,
    section_id: str,
    sections_by_id: Dict[str, Dict[str, object]],
) -> bool:
    """Guard the kind-agnostic ``number:N`` match against a cross-namespace target.

    A bare numbered/heading section has no namespace of its own and is treated as
    in-body. If the matched section instead carries an explicit attachment kind
    (a schedule/annex/appendix that only got a ``number:N`` alias), it must not
    satisfy a body reference -- that is the Schedule-N <-> Section-N collision.
    """
    section = sections_by_id.get(section_id)
    target_namespace = _kind_namespace(str(section.get("kind") or "")) if isinstance(section, dict) else None
    if target_namespace is None:
        target_namespace = NUMERIC_FALLBACK_NAMESPACE
    if reference_namespace is None:
        return True
    return target_namespace == reference_namespace


def _reference_status(items: Iterable[Dict[str, object]]) -> str:
    item_list = list(items)
    resolved = [item for item in item_list if item.get("section_id")]
    if resolved and len(resolved) == len(item_list):
        return "resolved"
    if resolved:
        return "partial"
    return "unresolved"


def _reference_numbers(value: str) -> List[str]:
    numbers: List[str] = []
    for part in REFERENCE_SEPARATOR_RE.split(value or ""):
        number = part.strip()
        numbers.extend(_reference_number_part_values(number))
    return numbers


def _reference_number_part_values(value: str) -> List[str]:
    number = value.strip()
    range_match = REFERENCE_NUMERIC_RANGE_RE.fullmatch(number)
    if range_match:
        start = int(range_match.group("start"))
        end = int(range_match.group("end"))
        if start <= end and end - start < MAX_REFERENCE_RANGE_SIZE:
            return [str(item) for item in range(start, end + 1)]
        return []
    if REFERENCE_NUMBER_RE.fullmatch(number):
        return [number]
    return []


def _canonical_kind(kind: str) -> str:
    return REFERENCE_KIND_ALIASES.get(str(kind or "").lower(), "")


def _is_self_heading_reference(match: re.Match[str], source_section_id: str | None, resolved_section_ids: List[str]) -> bool:
    if match.start() != 0 or not source_section_id or not resolved_section_ids:
        return False
    return all(section_id == source_section_id for section_id in resolved_section_ids)


def _string_dict(value: object) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _string_set(value: object) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item) for item in value if isinstance(item, str) and item}


def _section_lookup(value: object) -> Dict[str, Dict[str, object]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, dict)
    }


def _paragraph_id(paragraph: Paragraph) -> str | None:
    paragraph_id = paragraph.get("id")
    return str(paragraph_id) if paragraph_id is not None else None


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    results: List[str] = []
    for value in values:
        if not value or value in seen:
            continue
        results.append(value)
        seen.add(value)
    return results
