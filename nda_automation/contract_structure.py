from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List

from .heading_detection import (
    IDENTIFIER_PART_PATTERN,
    NUMBERED_NUMBER_PATTERN,
    ROMAN_NUMBER_PATTERN,
    block_clause_number,
    continuation_is_heading,
    parse_leading_number,
)
from .review_document import (
    SPLIT_CONTINUATION_KEY,
    SPLIT_PARENT_NUMBER_KEY,
    Paragraph,
)

STRUCTURE_VERSION = 2
REFERENCE_INDEX_VERSION = 2
# Clause-number grammar primitives (ROMAN_NUMBER_PATTERN, IDENTIFIER_PART_PATTERN,
# NUMBERED_NUMBER_PATTERN) are the single source in heading_detection and imported
# above; IDENTIFIER_PART_PATTERN is re-exported from this module so existing importers
# (reference_resolver) keep resolving.
EXPLICIT_NUMBER_PATTERN = rf"{IDENTIFIER_PART_PATTERN}(?:\.{IDENTIFIER_PART_PATTERN})*"
NUMBER_PART_RE = re.compile(r"^(?P<digits>\d+)(?P<suffix>[A-Za-z]+)$")
PARENTHETICAL_SUFFIX_RE = re.compile(r"^(?P<prefix>.*)\([A-Za-z0-9]+\)$")
PARENTHETICAL_PART_RE = re.compile(r"\(([A-Za-z0-9]+)\)")
OPERATIVE_SENTENCE_RE = re.compile(
    r"\b(?:shall|must|will|may|can|agrees?|undertakes?|covenants?|represents?|warrants?|"
    r"is|are|was|were|has|have|means|includes?|excludes?|not|appl(?:y|ies)|"
    r"surviv(?:e|es|ed|ing)|remain(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)

# Deterministic section-role cues. ADDITIVE: ``role`` is a label surfaced to the
# reviewer so a recital can be weighed differently from an operative clause; it never
# changes any decision logic. Ordering of the checks in ``_section_role`` is the
# precedence (signature > definitions > recital > operative > body).
# Contract-clause corroboration vocabulary. Unlike OPERATIVE_SENTENCE_RE (which is
# a loose grammatical-verb net that also fires on ordinary marketing prose -- "is",
# "are", "will", "not"), this is a deliberately CONTRACT-SPECIFIC lexicon: terms that
# a real agreement's body carries in abundance but a non-contract PDF (a sales deck's
# "1. Our Product / 2. Our Market") does not. It is the signal that lets the PDF
# confidence path tell a genuine numbered clause apart from decorative marketing
# numbering set in a large slide-title font. Kept broad on the contract side so a real
# NDA is NEVER starved of a match -- the gate errs toward trusting structure.
CONTRACT_SIGNAL_RE = re.compile(
    r"\b(?:confidential(?:ity)?|non-?disclosure|disclos(?:e|es|ed|ure|ing)|"
    r"proprietary|recipient|discloser|receiving\s+part(?:y|ies)|disclosing\s+part(?:y|ies)|"
    r"parties|hereto|hereby|herein|hereof|thereto|thereof|hereunder|"
    r"shall|agree(?:s|d|ment)?|obligations?|undertak(?:e|es|ing|ings)|covenants?|"
    r"warrant(?:s|y|ies)?|represents?|indemnif(?:y|ies|ication)|"
    r"governing\s+law|jurisdiction|terminat(?:e|es|ion|ing)|breach(?:es|ed)?|"
    r"liabilit(?:y|ies)|whereas|in\s+witness|counterparts?|severab(?:le|ility)|"
    r"waiver|binding|in\s+writing|force\s+and\s+effect|remed(?:y|ies)|"
    r"trade\s+secrets?|intellectual\s+property)\b",
    re.IGNORECASE,
)

# A non-contract PDF must show FEWER than this many distinct contract-signal hits
# across the whole document before its geometric numbered/heading sections are
# demoted from confident structure. A real NDA carries dozens of hits, so the margin
# is enormous and the exact threshold barely matters; a low value keeps the gate from
# ever firing on a genuine agreement.
_PDF_CONTRACT_SIGNAL_MIN = 2

RECITAL_CUE_RE = re.compile(r"\b(?:whereas|recital|background|in\s+consideration\s+of)\b", re.IGNORECASE)
DEFINITIONS_HEADING_RE = re.compile(r"\b(?:definitions?|interpretation|defined\s+terms?)\b", re.IGNORECASE)
SIGNATURE_CUE_RE = re.compile(
    r"\b(?:in\s+witness\s+whereof|signed\s+by|signature|for\s+and\s+on\s+behalf|"
    r"authorised\s+signatory|authorized\s+signatory|duly\s+authori[sz]ed|executed\s+as)\b",
    re.IGNORECASE,
)

EXPLICIT_HEADING_RE = re.compile(
    r"^\s*(?P<kind>clause|article|section|schedule|annex|annexure|appendix)\s+"
    rf"(?P<number>{EXPLICIT_NUMBER_PATTERN})(?P<separator>\s*[:.\-\u2013\u2014]\s*|\s+)"
    r"(?P<heading>.*)$",
    re.IGNORECASE,
)
NUMBERED_HEADING_RE = re.compile(
    rf"^\s*(?P<number>{NUMBERED_NUMBER_PATTERN})(?P<separator>\s*[:.\-\u2013\u2014]\s*|\s+)(?P<heading>.+)$"
)
UPPERCASE_PREFIX_RE = re.compile(
    r"^\s*(?P<heading>[A-Z][A-Z0-9 &,/()'\".\-]{2,90}):\s*(?P<body>.+)$"
)
UPPERCASE_STANDALONE_RE = re.compile(r"^[A-Z][A-Z0-9 &,/()'\".\-]{2,110}$")
TRAILING_NUMBER_DOT_RE = re.compile(r"\.$")

EXPLICIT_KIND_LABELS = {
    "annex": "Annex",
    "annexure": "Annexure",
    "appendix": "Appendix",
    "article": "Article",
    "clause": "Clause",
    "schedule": "Schedule",
    "section": "Section",
}

TITLE_WORDS = {
    "agreement",
    "disclosure",
    "non-disclosure",
    "confidentiality",
    "nda",
}


@dataclass
class _SectionCandidate:
    position: int
    kind: str
    label: str
    number: str | None
    heading: str
    level: int
    confidence: str
    heading_text: str
    source: Dict[str, object] | None = None


def build_contract_structure(paragraphs: List[Paragraph]) -> Dict[str, object]:
    """Build a document-specific map of contract headings and paragraph ranges."""
    document_paragraphs = [paragraph for paragraph in paragraphs if str(paragraph.get("text", "")).strip()]
    candidates = _detect_section_candidates(document_paragraphs)
    sections: List[Dict[str, object]] = []

    if document_paragraphs and (not candidates or candidates[0].position > 0):
        first_candidate_position = candidates[0].position if candidates else len(document_paragraphs)
        preamble_paragraphs = document_paragraphs[:first_candidate_position]
        if preamble_paragraphs:
            sections.append(_section_dict(
                section_id="section-1",
                kind="preamble",
                label="Preamble",
                number=None,
                heading="Preamble",
                level=0,
                paragraphs=preamble_paragraphs,
                parent_id=None,
                confidence="high",
                heading_text="Preamble",
                source=None,
            ))

    for candidate_index, candidate in enumerate(candidates):
        next_position = (
            candidates[candidate_index + 1].position
            if candidate_index + 1 < len(candidates)
            else len(document_paragraphs)
        )
        section_paragraphs = document_paragraphs[candidate.position:next_position]
        if not section_paragraphs:
            continue

        section_id = f"section-{len(sections) + 1}"
        parent_id = _find_parent_id(sections, candidate)
        sections.append(_section_dict(
            section_id=section_id,
            kind=candidate.kind,
            label=candidate.label,
            number=candidate.number,
            heading=candidate.heading,
            level=candidate.level,
            paragraphs=section_paragraphs,
            parent_id=parent_id,
            confidence=candidate.confidence,
            heading_text=candidate.heading_text,
            source=candidate.source,
        ))

    _demote_uncorroborated_pdf_sections(sections, document_paragraphs)

    aliases, ambiguous_alias_keys = _build_aliases(sections)
    reference_index = _build_reference_index(sections, aliases, ambiguous_alias_keys)
    mapped_paragraph_ids = {
        paragraph_id
        for section in sections
        for paragraph_id in section.get("paragraph_ids", [])
        if isinstance(paragraph_id, str)
    }
    all_paragraph_ids = {
        _paragraph_id(paragraph)
        for paragraph in document_paragraphs
        if _paragraph_id(paragraph) is not None
    }

    return {
        "version": STRUCTURE_VERSION,
        "sections": sections,
        "aliases": aliases,
        "reference_index": reference_index,
        "stats": {
            "section_count": len(sections),
            "mapped_paragraph_count": len(mapped_paragraph_ids),
            "unmapped_paragraph_count": len(all_paragraph_ids - mapped_paragraph_ids),
            **_source_stats(document_paragraphs, sections),
        },
    }


def _detect_section_candidates(paragraphs: List[Paragraph]) -> List[_SectionCandidate]:
    candidates: List[_SectionCandidate] = []
    seen_positions = set()
    for position, paragraph in enumerate(paragraphs):
        candidate = _candidate_for_paragraph(position, paragraph, candidates)
        if candidate is None or position in seen_positions:
            continue
        # PDF source-backed trust tier: a heading the regex detectors accepted on a
        # PDF paragraph becomes source-backed ONLY when its captured geometry (a
        # larger heading font) corroborates the regex. A bare regex match with no
        # geometric corroboration (e.g. an address digit "145 Main Street" misread as
        # a clause) is left WITHOUT a ``source`` mapping, so the source-backed gate
        # stays closed for it and the phantom never reaches the verifier/budget.
        _apply_pdf_confidence(candidate, paragraph)
        candidates.append(candidate)
        seen_positions.add(position)
    return candidates


# A heading font must exceed the page body font by this factor to corroborate a PDF
# regex heading match. Mirrors pdf_text._HEADING_FONT_FACTOR so the geometry trust
# tier agrees with the splitter's own heading-font cue.
_PDF_HEADING_FONT_FACTOR = 1.15


def _apply_pdf_confidence(candidate: _SectionCandidate, paragraph: Paragraph) -> None:
    """Mark a PDF regex heading as source-backed IFF geometry corroborates it.

    The DISTINCT PDF trust tier. The keystone guard: a section is promoted to
    ``source={"kind": "pdf_confident", ...}`` ONLY when BOTH hold:

      * REGEX — the heading was already accepted by one of the structural detectors
        (this function only runs on an accepted ``candidate``), i.e. it is an
        explicit/numbered/uppercase heading, NOT arbitrary prose; AND
      * GEOMETRY — the captured per-paragraph ``pdf_geometry`` shows the heading line
        set in a font MEANINGFULLY LARGER than the page's dominant body font
        (``heading_font_ratio >= _PDF_HEADING_FONT_FACTOR``).

    A bare regex match with no geometry corroboration (no ``pdf_geometry``, no body
    font, or a body-sized heading font) is left WITHOUT a ``source``, so it is NOT
    source-backed — this is precisely what keeps a phantom heading (an address line,
    a body-sized "Clause 145" misread) out of the trusted structural features. The
    geometry is derived from the document's own typography, never from the untrusted
    text, so it is not a new injection surface.
    """
    geometry = paragraph.get("pdf_geometry")
    if not isinstance(geometry, dict):
        return
    ratio = geometry.get("heading_font_ratio")
    if not isinstance(ratio, (int, float)) or ratio < _PDF_HEADING_FONT_FACTOR:
        return
    source: Dict[str, object] = {"kind": "pdf_confident"}
    font_size = geometry.get("font_size")
    if isinstance(font_size, (int, float)):
        source["font_size"] = font_size
    body_font = geometry.get("body_font")
    if isinstance(body_font, (int, float)):
        source["body_font"] = body_font
    source["heading_font_ratio"] = float(ratio)
    if isinstance(paragraph.get("source_part"), str):
        source["source_part"] = paragraph["source_part"]
    if isinstance(paragraph.get("source_index"), int):
        source["source_index"] = paragraph["source_index"]
    candidate.source = source


def _paragraph_is_pdf_geometry(paragraph: Paragraph) -> bool:
    """True when a paragraph carries PDF layout geometry (``pdf_geometry``), i.e. it
    came from the geometry-aware PDF extractor rather than a DOCX or plain-text source."""
    return isinstance(paragraph.get("pdf_geometry"), dict)


def _paragraph_has_docx_metadata(paragraph: Paragraph) -> bool:
    """True when a paragraph carries any DOCX structural metadata. A DOCX-sourced parse
    is authoritative and must be left entirely untouched by the PDF demotion pass -- this
    is the guard that keeps the pass from ever regressing a Word document."""
    for key in ("source_kind", "style_id", "heading_level", "outline_level", "structure_number"):
        value = paragraph.get(key)
        if isinstance(value, (str, int)) and str(value) != "":
            return True
    return isinstance(paragraph.get("numbering"), dict)


def _document_is_pdf_only(paragraphs: List[Paragraph]) -> bool:
    """True for a document whose paragraphs come from the PDF geometry extractor and
    carry NO DOCX structural metadata anywhere. The demotion pass only ever considers
    such documents, so a DOCX (or DOCX-with-a-stray-PDF-paragraph) parse is never touched."""
    if any(_paragraph_has_docx_metadata(paragraph) for paragraph in paragraphs):
        return False
    return any(_paragraph_is_pdf_geometry(paragraph) for paragraph in paragraphs)


def _document_contract_signal_count(paragraphs: List[Paragraph]) -> int:
    """Count distinct contract-clause signal hits across the whole document body. A real
    agreement carries many; a non-contract PDF (a marketing deck) carries ~none."""
    combined = " ".join(str(paragraph.get("text", "")) for paragraph in paragraphs)
    return len(CONTRACT_SIGNAL_RE.findall(combined))


def _demote_uncorroborated_pdf_sections(
    sections: List[Dict[str, object]], paragraphs: List[Paragraph]
) -> None:
    """Demote confident numbered/heading sections on a NON-CONTRACT PDF in place.

    Problem this closes: the PDF confidence path corroborates a geometric number by FONT
    SIZE alone (a heading set larger than the body). On a sales deck that heuristic is
    exactly inverted -- a slide's marketing title (``1. Our Product``) IS set in a large
    font, so the number is wrongly promoted to a high-confidence, source-backed contract
    clause. Font size is a heading cue, not a CONTRACT cue.

    Fix: require corroborating contract-clause signals before a PDF geometric number is
    trusted as a confident clause. When a document is PDF-only (no DOCX metadata) AND the
    whole document shows fewer than ``_PDF_CONTRACT_SIGNAL_MIN`` contract-signal hits --
    i.e. it is not a contract at all -- every numbered/heading section is demoted to
    ``confidence="low"`` and stripped of its ``pdf_confident`` source-backed trust tier.

    Conservative in BOTH directions:
      * A real NDA (PDF or DOCX) carries dozens of contract signals, clears the threshold,
        and is left completely untouched -- its clauses keep high confidence.
      * A DOCX parse is excluded up front (``_document_is_pdf_only``), so Word documents
        never regress.
      * Only PDF geometric numbering on a genuinely non-contract document is demoted.
    """
    if not sections:
        return
    if not _document_is_pdf_only(paragraphs):
        return
    if _document_contract_signal_count(paragraphs) >= _PDF_CONTRACT_SIGNAL_MIN:
        return
    for section in sections:
        if section.get("kind") in {"numbered", "heading"}:
            if section.get("confidence") in {"high", "medium"}:
                section["confidence"] = "low"
            section.pop("source", None)


def _candidate_for_paragraph(
    position: int,
    paragraph: Paragraph,
    prior_candidates: List[_SectionCandidate] | None = None,
) -> _SectionCandidate | None:
    prior_candidates = prior_candidates or []
    text = _collapse_whitespace(str(paragraph.get("text", "")))
    if not text:
        return None

    # Context-aware gate: a soft-return CONTINUATION of an already-numbered block
    # (stamped by align_document_paragraphs) is wrapped body text, not a new
    # heading -- unless it carries its own explicit-separator number that DIFFERS
    # from the parent clause. This is the single check that replaces the second,
    # independent re-derivation that used to undo the upstream fix; the metadata
    # path below is skipped for continuations because the parent's clause number
    # is not the continuation's own.
    if _is_split_continuation(paragraph):
        parent_number = _split_parent_number(paragraph)
        if not continuation_is_heading(text, parent_number):
            return None
        return _continuation_heading_candidate(position, text)

    metadata_candidate = _candidate_from_source_metadata(position, paragraph, text)
    if metadata_candidate is not None:
        return metadata_candidate

    explicit_match = EXPLICIT_HEADING_RE.match(text)
    if explicit_match:
        kind = explicit_match.group("kind").lower()
        number = TRAILING_NUMBER_DOT_RE.sub("", explicit_match.group("number").strip())
        heading = _clean_heading(explicit_match.group("heading")) or _display_kind(kind)
        if not _looks_like_explicit_heading(number, heading, explicit_match.group("separator")):
            return None
        label = f"{_display_kind(kind)} {number}"
        return _SectionCandidate(
            position=position,
            kind=kind,
            label=label,
            number=number,
            heading=heading,
            level=_level_for_number(number),
            confidence="high",
            heading_text=_preview(text),
        )

    numbered_match = NUMBERED_HEADING_RE.match(text)
    if numbered_match and _looks_like_numbered_heading(
        numbered_match.group("number"),
        numbered_match.group("heading"),
        numbered_match.group("separator"),
    ):
        number = TRAILING_NUMBER_DOT_RE.sub("", numbered_match.group("number").strip())
        heading = _clean_heading(numbered_match.group("heading"))
        level = _level_for_number(number)
        # Bare-outline over-promotion guard: a lone sub-list marker -- a parenthetical
        # like "(ii)" or a bare single letter/roman like "A"/"IV" -- intrinsically lives
        # *under* a parent and can never legitimately be the first top-level section.
        # Without prior structural context it is a stray list bullet; promoting it to a
        # level-1 section lets its paragraph range swallow the rest of the document
        # (observed: one bullet swallowing 28% of a real doc). Only accept it once some
        # genuine numbered/explicit outline context already exists upstream.
        if (
            level == 1
            and _is_bare_outline_marker(number)
            and not _has_outline_context(prior_candidates)
        ):
            return None
        return _SectionCandidate(
            position=position,
            kind="numbered",
            label=number,
            number=number,
            heading=heading,
            level=level,
            confidence="high",
            heading_text=_preview(text),
        )

    uppercase_prefix_match = UPPERCASE_PREFIX_RE.match(text)
    if uppercase_prefix_match:
        heading = _clean_heading(uppercase_prefix_match.group("heading"))
        if _looks_like_uppercase_heading(heading):
            return _SectionCandidate(
                position=position,
                kind="heading",
                label=heading,
                number=None,
                heading=heading,
                level=1,
                confidence="medium",
                heading_text=_preview(text),
            )

    if UPPERCASE_STANDALONE_RE.match(text) and _looks_like_uppercase_heading(text):
        if position == 0 and _looks_like_document_title(text):
            return None
        heading = _clean_heading(text)
        return _SectionCandidate(
            position=position,
            kind="heading",
            label=heading,
            number=None,
            heading=heading,
            level=1,
            confidence="medium",
            heading_text=_preview(text),
        )

    return None


def _candidate_from_source_metadata(position: int, paragraph: Paragraph, text: str) -> _SectionCandidate | None:
    source = _source_metadata(paragraph)
    structure_number = _source_structure_number(paragraph)
    if structure_number:
        # The paragraph carries a Word-autonumber. Reconcile it with any literal
        # number the author typed into the run via the SHARED block_clause_number
        # (the same definition align_document_paragraphs used for split provenance,
        # so the two layers agree): a literal "2. Second." under autonumber "1"
        # yields section "2", while a numbered paragraph whose run carries no
        # literal prefix keeps the autonumber. This branch only runs when a real
        # autonumber exists, so prose without metadata still defers to the careful
        # text classifiers below.
        effective_number = block_clause_number(text, structure_number)
        heading = _clean_heading(_strip_leading_number(text, effective_number)) or _clean_heading(text)
        return _SectionCandidate(
            position=position,
            kind="numbered",
            label=effective_number,
            number=effective_number,
            heading=heading,
            level=_source_number_level(paragraph, effective_number),
            confidence="high",
            heading_text=_preview(_source_heading_text(paragraph, text)),
            source=source,
        )

    heading_level = paragraph.get("heading_level")
    # A continuation piece inherits its source block's ``heading_level`` (the
    # whole Word paragraph's style), but a wrapped continuation is not itself a
    # styled heading -- so the continuation gate runs before this and this path is
    # never reached for continuations.
    if isinstance(heading_level, int) and heading_level > 0:
        heading = _clean_heading(text)
        return _SectionCandidate(
            position=position,
            kind="heading",
            label=heading,
            number=None,
            heading=heading,
            level=heading_level,
            confidence="high",
            heading_text=_preview(text),
            source=source,
        )

    return None


def _is_split_continuation(paragraph: Paragraph) -> bool:
    return bool(paragraph.get(SPLIT_CONTINUATION_KEY))


def _split_parent_number(paragraph: Paragraph) -> str:
    value = paragraph.get(SPLIT_PARENT_NUMBER_KEY)
    return value.strip() if isinstance(value, str) else ""


def _continuation_heading_candidate(position: int, text: str) -> _SectionCandidate | None:
    """Build a section for a continuation that the unified detector accepted as a
    genuinely new heading -- a distinct, explicit-separator numbered clause that
    shared one Word paragraph via soft returns (e.g. ``3. Third.``). The number is
    taken from the continuation's own text, and its literal prefix is stripped
    from the heading, mirroring the numbered-heading path."""
    leading = parse_leading_number(text)
    if leading is None:
        return None
    number = leading.number
    heading = _clean_heading(_strip_leading_number(text, number)) or _clean_heading(text)
    return _SectionCandidate(
        position=position,
        kind="numbered",
        label=number,
        number=number,
        heading=heading,
        level=_level_for_number(number),
        confidence="high",
        heading_text=_preview(text),
    )


def _section_dict(
    *,
    section_id: str,
    kind: str,
    label: str,
    number: str | None,
    heading: str,
    level: int,
    paragraphs: List[Paragraph],
    parent_id: str | None,
    confidence: str,
    heading_text: str,
    source: Dict[str, object] | None = None,
) -> Dict[str, object]:
    paragraph_ids = [
        paragraph_id
        for paragraph_id in (_paragraph_id(paragraph) for paragraph in paragraphs)
        if paragraph_id is not None
    ]
    first_paragraph = paragraphs[0]
    last_paragraph = paragraphs[-1]

    section: Dict[str, object] = {
        "id": section_id,
        "kind": kind,
        "label": label,
        "heading": heading,
        "number": number,
        "level": level,
        "role": _section_role(kind, number, heading, paragraphs),
        "paragraph_ids": paragraph_ids,
        "start_paragraph_id": _paragraph_id(first_paragraph),
        "end_paragraph_id": _paragraph_id(last_paragraph),
        "start_index": _paragraph_index(first_paragraph),
        "end_index": _paragraph_index(last_paragraph),
        "parent_id": parent_id,
        "confidence": confidence,
        "heading_text": heading_text,
    }
    if source:
        section["source"] = source
    return section


def _section_role(
    kind: str,
    number: str | None,
    heading: str,
    paragraphs: List[Paragraph],
) -> str:
    """Classify a section into a deterministic, ADDITIVE role label.

    Roles (precedence order):
      * ``signature``   -- a signature-block cue (``IN WITNESS WHEREOF``, ``Signed by``,
                           ``for and on behalf``, an authorised-signatory line).
      * ``definitions`` -- the heading matches a definitions/interpretation cue.
      * ``recital``     -- a ``preamble`` section, or one carrying a WHEREAS/recital cue.
      * ``operative``   -- a numbered clause whose body carries an operative verb
                           (via the shared ``OPERATIVE_SENTENCE_RE``).
      * ``body``        -- everything else.

    Purely a hint for the reviewer (weigh a recital differently from an operative
    clause). It is derived from the deterministic structure + the section's own text,
    never alters any verdict, and is computed best-effort (any cue miss falls back to
    ``body``)."""
    heading_text = str(heading or "")
    body_text = " ".join(str(paragraph.get("text", "")) for paragraph in paragraphs)
    combined = f"{heading_text} {body_text}"

    if SIGNATURE_CUE_RE.search(combined):
        return "signature"
    if DEFINITIONS_HEADING_RE.search(heading_text):
        return "definitions"
    if kind == "preamble" or RECITAL_CUE_RE.search(combined):
        return "recital"
    if number and OPERATIVE_SENTENCE_RE.search(body_text):
        return "operative"
    return "body"


def _find_parent_id(sections: List[Dict[str, object]], candidate: _SectionCandidate) -> str | None:
    if candidate.number is None:
        return None

    for parent_number in _parent_number_candidates(candidate.number):
        parent_id = _find_section_id_by_number(sections, parent_number, candidate.level)
        if parent_id is not None:
            return parent_id

    for section in reversed(sections):
        parent_number = section.get("number")
        if not isinstance(parent_number, str):
            continue
        if candidate.number.startswith(parent_number + ".") and int(section.get("level", 0)) < candidate.level:
            section_id = section.get("id")
            return str(section_id) if section_id is not None else None
    return None


def _build_aliases(sections: Iterable[Dict[str, object]]) -> tuple[List[Dict[str, str]], List[str]]:
    """Map alias keys to sections, refusing to bind a key shared by >1 section.

    When a document restarts numbering -- e.g. an appended Exhibit/Order Form whose
    "Section 2" follows the main body's "Section 2" -- a kind/number alias key like
    ``section:2`` is claimed by more than one section. Silently binding the key to the
    *first* occurrence (the old first-write-wins behaviour) makes every "Section 2"
    cross-reference resolve to the wrong target with full confidence and no ambiguity
    signal, which lets an unapproved governing law referenced via a duplicate section be
    silently cleared. Instead, a key claimed by two or more *distinct* sections is
    recorded as ambiguous and left out of the binding map, so the resolver treats those
    references as unresolved rather than mis-resolved (the conservative-correct stance
    the resolver already takes for the Schedule<->Section namespace collision)."""
    first_section_for_key: Dict[str, Dict[str, str]] = {}
    ambiguous_keys: set[str] = set()
    key_order: List[str] = []
    for section in sections:
        section_id = str(section.get("id", ""))
        if not section_id:
            continue
        label = str(section.get("label", ""))
        heading = str(section.get("heading", ""))
        kind = str(section.get("kind", ""))
        number = section.get("number")

        alias_keys: List[str] = []
        if isinstance(number, str) and number:
            alias_keys.append(f"number:{number.lower()}")
            if kind in EXPLICIT_KIND_LABELS:
                alias_keys.append(f"{kind}:{number.lower()}")
        heading_key = _normalize_heading_key(heading)
        if heading_key:
            alias_keys.append(f"heading:{heading_key}")

        for key in alias_keys:
            existing = first_section_for_key.get(key)
            if existing is None:
                first_section_for_key[key] = {"key": key, "section_id": section_id, "label": label}
                key_order.append(key)
            elif existing["section_id"] != section_id:
                # A second, distinct section claims this key -- the key is ambiguous and
                # must not bind to either occurrence.
                ambiguous_keys.add(key)

    aliases = [
        first_section_for_key[key]
        for key in key_order
        if key not in ambiguous_keys
    ]
    return aliases, sorted(ambiguous_keys)


def _build_reference_index(
    sections: Iterable[Dict[str, object]],
    aliases: Iterable[Dict[str, str]],
    ambiguous_alias_keys: Iterable[str] = (),
) -> Dict[str, object]:
    section_lookup: Dict[str, Dict[str, object]] = {}
    paragraph_lookup: Dict[str, str] = {}
    section_ids: List[str] = []

    for section in sections:
        section_id = str(section.get("id") or "")
        if not section_id:
            continue
        section_ids.append(section_id)
        section_lookup[section_id] = _resolver_section_record(section)
        for paragraph_id in section.get("paragraph_ids", []):
            if isinstance(paragraph_id, str) and paragraph_id:
                paragraph_lookup[paragraph_id] = section_id

    return {
        "version": REFERENCE_INDEX_VERSION,
        "section_ids": section_ids,
        "sections_by_id": section_lookup,
        "alias_to_section_id": {
            alias["key"]: alias["section_id"]
            for alias in aliases
            if isinstance(alias.get("key"), str) and isinstance(alias.get("section_id"), str)
        },
        # Alias keys claimed by more than one section (e.g. a "Section 2" that recurs in
        # an appended Exhibit with restarted numbering). The resolver must treat
        # references to these as unresolved rather than silently bind to one occurrence.
        "ambiguous_alias_keys": sorted(
            str(key) for key in ambiguous_alias_keys if isinstance(key, str) and key
        ),
        "paragraph_to_section_id": paragraph_lookup,
    }


def _resolver_section_record(section: Dict[str, object]) -> Dict[str, object]:
    record: Dict[str, object] = {
        "id": str(section.get("id") or ""),
        "kind": str(section.get("kind") or ""),
        "number": section.get("number") if isinstance(section.get("number"), str) else None,
        "label": str(section.get("label") or ""),
        "heading": str(section.get("heading") or ""),
        "level": int(section.get("level", 0)) if isinstance(section.get("level"), int) else 0,
        "role": str(section.get("role") or "body"),
        "paragraph_ids": [
            paragraph_id
            for paragraph_id in section.get("paragraph_ids", [])
            if isinstance(paragraph_id, str)
        ],
        "start_index": section.get("start_index") if isinstance(section.get("start_index"), int) else None,
        "end_index": section.get("end_index") if isinstance(section.get("end_index"), int) else None,
        "parent_id": section.get("parent_id") if isinstance(section.get("parent_id"), str) else None,
    }
    if isinstance(section.get("source"), dict):
        record["source"] = section["source"]
    return record


def _source_stats(paragraphs: List[Paragraph], sections: List[Dict[str, object]]) -> Dict[str, object]:
    source_kinds = {
        str(paragraph.get("source_kind") or "")
        for paragraph in paragraphs
        if paragraph.get("source_kind")
    }
    source_parts = {
        str(paragraph.get("source_part") or "")
        for paragraph in paragraphs
        if paragraph.get("source_part")
    }
    return {
        "docx_numbered_paragraph_count": sum(1 for paragraph in paragraphs if isinstance(paragraph.get("numbering"), dict)),
        "docx_heading_paragraph_count": sum(1 for paragraph in paragraphs if isinstance(paragraph.get("heading_level"), int)),
        "table_paragraph_count": sum(1 for paragraph in paragraphs if isinstance(paragraph.get("table"), dict)),
        "source_backed_section_count": sum(1 for section in sections if isinstance(section.get("source"), dict)),
        "source_kinds": sorted(source_kinds),
        "source_parts": sorted(source_parts),
    }


def _looks_like_numbered_heading(number: str, heading: str, separator: str = "") -> bool:
    cleaned = _clean_heading(heading)
    if not cleaned:
        return False
    if _requires_strict_outline_heading(number):
        return _looks_like_short_heading(cleaned)
    if len(cleaned) <= 120:
        return True
    if ":" in cleaned[:90]:
        return True
    # Run-in clause rescue: a genuine flat-DOCX / PDF-reconstructed clause reads
    # "5. The Receiving Party shall not disclose ..." -- a real clause number,
    # followed by an explicit punctuation separator (the deliberate marker, not the
    # bare whitespace of body prose like "5 years from the date"), running straight
    # into a long sentence that begins like a clause (an uppercase first letter, the
    # start of a real sentence -- not a mid-sentence reference fragment such as
    # "5 years ..."). Capturing it as a clause is correct structure; the explicit
    # separator + uppercase-sentence-start guard keeps arbitrary numbered body prose
    # out. Strict-outline markers ((ii), (a), bare letters/romans) never reach here.
    return _has_explicit_separator(separator) and _starts_like_clause_sentence(cleaned)


def _has_explicit_separator(separator: str) -> bool:
    """True when the marker was followed by deliberate punctuation (``.``/``:``/dash),
    not bare whitespace. A clause marker reads ``5. The ...``; body prose such as
    ``5 years from the date`` separates the number from the next word with whitespace
    only and must not be promoted to a clause heading."""
    return bool(re.search(r"[:.\-–—]", separator or ""))


def _starts_like_clause_sentence(cleaned: str) -> bool:
    """True when a (long) run-in heading begins like a real clause sentence: its first
    alphabetic character is uppercase. Mid-sentence reference fragments and unit phrases
    (``years from the date``, ``business days notice``) begin lowercase and are rejected."""
    first_alpha = next((character for character in cleaned if character.isalpha()), "")
    return bool(first_alpha and first_alpha.isupper())


def _looks_like_explicit_heading(number: str, heading: str, separator: str) -> bool:
    cleaned = _clean_heading(heading)
    if not cleaned:
        return True
    if re.match(r"^(?:and|or)\b", cleaned, flags=re.IGNORECASE):
        return False
    if separator.strip():
        return len(cleaned) <= 160 or ":" in cleaned[:90]
    if _requires_strict_outline_heading(number):
        return _looks_like_short_heading(cleaned)
    return _looks_like_short_heading(cleaned) or not re.search(OPERATIVE_SENTENCE_RE, cleaned)


def _is_bare_outline_marker(number: str) -> bool:
    """True for a single-part sub-list marker that has no own parent in its identifier:
    a parenthetical like ``(ii)``/``(a)``, or a bare single letter/roman like ``A``/``IV``.
    Compound markers (``1(a)``, ``5.1``, ``II.A``) carry their own parent and are excluded;
    digit-led top-level numbers (``5``, ``10``) are real clauses and are excluded."""
    normalized_number = str(number or "").strip()
    if not normalized_number:
        return False
    if "." in normalized_number:
        return False
    parenthetical_match = PARENTHETICAL_SUFFIX_RE.match(normalized_number)
    if parenthetical_match:
        # A lone "(ii)" has an empty prefix; "1(a)" has prefix "1" and is not bare.
        return not parenthetical_match.group("prefix").strip()
    return bool(
        re.fullmatch(rf"(?:{ROMAN_NUMBER_PATTERN}|[A-Za-z])", normalized_number, flags=re.IGNORECASE)
    )


def _has_outline_context(prior_candidates: List[_SectionCandidate]) -> bool:
    """True when at least one genuine numbered/explicit section already precedes this one,
    i.e. the document is structured and a sub-list marker has plausible context. A lone
    bullet in otherwise unstructured prose has no such context."""
    return any(
        candidate.number and candidate.kind in {"numbered", *EXPLICIT_KIND_LABELS}
        for candidate in prior_candidates
    )


def _requires_strict_outline_heading(number: str) -> bool:
    normalized_number = str(number or "").strip()
    if not normalized_number:
        return False
    if "(" in normalized_number or ")" in normalized_number:
        return True
    return "." not in normalized_number and bool(
        re.fullmatch(rf"(?:{ROMAN_NUMBER_PATTERN}|[A-Za-z])", normalized_number, flags=re.IGNORECASE)
    )


def _looks_like_short_heading(text: str) -> bool:
    cleaned = _clean_heading(text)
    if not cleaned or len(cleaned) > 90:
        return False
    if re.search(OPERATIVE_SENTENCE_RE, cleaned):
        return False
    words = [word for word in re.split(r"\s+", cleaned) if word]
    if len(words) > 8:
        return False
    first_alpha = next((character for character in cleaned if character.isalpha()), "")
    return bool(first_alpha and first_alpha.isupper())


def _looks_like_uppercase_heading(text: str) -> bool:
    cleaned = _clean_heading(text)
    if len(cleaned) < 3:
        return False
    letters = [character for character in cleaned if character.isalpha()]
    if len(letters) < 3:
        return False
    uppercase_letters = [character for character in letters if character.isupper()]
    return len(uppercase_letters) / len(letters) >= 0.85


def _looks_like_document_title(text: str) -> bool:
    key = _normalize_heading_key(text)
    words = set(key.split())
    return bool(words & TITLE_WORDS) and len(words) <= 8


def _level_for_number(number: str | None) -> int:
    if not number:
        return 1
    parts = _number_parts(number)
    if not parts:
        return 1
    return len(parts) + sum(1 for part in parts if _strip_letter_suffix(part) is not None)


def _parent_number_candidates(number: str) -> List[str]:
    candidates: List[str] = []
    queue = [number]
    seen = {number}

    while queue:
        current = queue.pop(0)
        for parent_number in _immediate_parent_numbers(current):
            if parent_number in seen:
                continue
            candidates.append(parent_number)
            queue.append(parent_number)
            seen.add(parent_number)
    return candidates


def _immediate_parent_numbers(number: str) -> List[str]:
    parents: List[str] = []
    normalized_number = str(number or "").strip()
    if not normalized_number:
        return parents

    parenthetical_match = PARENTHETICAL_SUFFIX_RE.match(normalized_number)
    if parenthetical_match:
        parents.append(parenthetical_match.group("prefix"))
    else:
        raw_parts = [part for part in normalized_number.split(".") if part]
        if raw_parts:
            stripped_last_part = _strip_letter_suffix(raw_parts[-1])
            if stripped_last_part:
                parents.append(".".join([*raw_parts[:-1], stripped_last_part]))
            if len(raw_parts) > 1:
                parents.append(".".join(raw_parts[:-1]))
    return [parent for parent in parents if parent and parent != number]


def _find_section_id_by_number(
    sections: List[Dict[str, object]],
    parent_number: str,
    candidate_level: int,
) -> str | None:
    for section in reversed(sections):
        section_number = section.get("number")
        if section_number != parent_number or int(section.get("level", 0)) >= candidate_level:
            continue
        section_id = section.get("id")
        return str(section_id) if section_id is not None else None
    return None


def _source_metadata(paragraph: Paragraph) -> Dict[str, object] | None:
    source: Dict[str, object] = {}
    for key in ("source_kind", "style_id", "style_name", "heading_level", "outline_level", "structure_label"):
        value = paragraph.get(key)
        if isinstance(value, (str, int)) and str(value) != "":
            source[key] = value
    for key in ("numbering", "table"):
        value = paragraph.get(key)
        if isinstance(value, dict):
            source[key] = value
    if "source_index" in paragraph and isinstance(paragraph.get("source_index"), int):
        source["source_index"] = paragraph["source_index"]
    if "source_part" in paragraph and isinstance(paragraph.get("source_part"), str):
        source["source_part"] = paragraph["source_part"]
    return source or None


def _source_structure_number(paragraph: Paragraph) -> str:
    numbering = paragraph.get("numbering")
    direct_number = paragraph.get("structure_number")
    if isinstance(direct_number, str) and direct_number.strip():
        cleaned = _clean_source_number(direct_number)
        if cleaned:
            return cleaned
        # An explicit structure number the grammar rejects (e.g. a nested
        # "1.(c)(i)") must NOT be re-derived from the raw numbering label -- the
        # contract folds such a paragraph into its parent (see the AirIndia
        # sub-clause test). The ONE exception is a custom ``lvlText`` template
        # ("Article %1"), whose rendered label is otherwise unrecognisable: recover
        # the number from the template only (D12), never from the plain label.
        if isinstance(numbering, dict):
            return _number_from_level_text(numbering)
        return ""
    if not isinstance(numbering, dict):
        return ""
    number_format = str(numbering.get("format") or "")
    if number_format in {"bullet", "none"}:
        return ""
    label = str(numbering.get("label") or "").strip()
    cleaned = _clean_source_number(label)
    if cleaned:
        return cleaned
    # Custom ``lvlText`` templates mix literal words with the ``%N`` counter (e.g.
    # "Article %1" -> label "Article 1"). The bare dotted-identifier match rejects the
    # space + word, so the autonumbered section was dropped/mis-levelled in the
    # Structure tab even though the reconstruction ``::before`` shows "Article 1"
    # (D12). Recover the dotted number path from the level template so the section is
    # recognised; ``_source_number_level`` still takes the depth from the numbering
    # ilvl, so "Article %1" lands at the level Word gives it.
    return _number_from_level_text(numbering)


def _number_from_level_text(numbering: Dict[str, object]) -> str:
    """Recover the dotted number path from a custom ``lvlText`` template (D12).

    ``level_text`` is the raw Word template ("Article %1", "Section %1.%2"); its
    ``%N`` placeholders were replaced by the rendered counter values in ``label``.
    Rebuild a regex from the template -- literal segments stay literal, each ``%N``
    becomes an identifier capture -- match it against the label, and join the
    captured identifiers with dots. Returns "" when the label yields no recoverable
    identifier, so a pure-literal template ("Recital") stays unnumbered."""
    level_text = str(numbering.get("level_text") or "")
    label = str(numbering.get("label") or "").strip()
    if "%" not in level_text or not label:
        return ""
    segments = re.split(r"%\d+", level_text)
    pattern = "(.+?)".join(re.escape(segment) for segment in segments)
    match = re.fullmatch(pattern, label)
    if match is None:
        return ""
    tokens: List[str] = []
    for raw in match.groups():
        token = _clean_source_number(str(raw or "").strip())
        if token:
            tokens.append(token)
    return ".".join(tokens)


def _clean_source_number(value: str) -> str:
    cleaned = re.sub(r"^[^\w(]+|[^\w)]+$", "", value or "").strip()
    return cleaned if re.fullmatch(EXPLICIT_NUMBER_PATTERN, cleaned, flags=re.IGNORECASE) else ""


def _source_number_level(paragraph: Paragraph, number: str) -> int:
    numbering = paragraph.get("numbering")
    if isinstance(numbering, dict) and isinstance(numbering.get("level"), int):
        return int(numbering["level"]) + 1
    return _level_for_number(number)


def _source_heading_text(paragraph: Paragraph, text: str) -> str:
    structure_label = paragraph.get("structure_label")
    if isinstance(structure_label, str) and structure_label.strip():
        return f"{structure_label.strip()} {text}"
    return text


def _strip_leading_number(text: str, number: str) -> str:
    pattern = rf"^\s*{re.escape(number)}(?:\s*[:.)\-\u2013\u2014]\s*|\s+)"
    return re.sub(pattern, "", text, count=1)


def _number_parts(number: str | None) -> List[str]:
    parts: List[str] = []
    for raw_part in str(number or "").split("."):
        part = raw_part.strip()
        if not part:
            continue
        prefix = PARENTHETICAL_SUFFIX_RE.sub(r"\g<prefix>", part).strip()
        if prefix:
            parts.append(prefix)
        parts.extend(match.group(1) for match in PARENTHETICAL_PART_RE.finditer(part))
    return parts


def _strip_letter_suffix(part: str) -> str | None:
    match = NUMBER_PART_RE.match(part)
    return match.group("digits") if match else None


def _display_kind(kind: str) -> str:
    return EXPLICIT_KIND_LABELS.get(kind.lower(), kind.capitalize())


def _paragraph_id(paragraph: Paragraph) -> str | None:
    paragraph_id = paragraph.get("id")
    return str(paragraph_id) if paragraph_id is not None else None


def _paragraph_index(paragraph: Paragraph) -> int | None:
    index = paragraph.get("index")
    return index if isinstance(index, int) else None


def _clean_heading(text: str) -> str:
    return _collapse_whitespace(text).strip(" .:-")


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_heading_key(text: str) -> str:
    lowered = text.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", lowered)
    return _collapse_whitespace(normalized)


def _preview(text: str, limit: int = 220) -> str:
    collapsed = _collapse_whitespace(text)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."
