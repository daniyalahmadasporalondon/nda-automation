from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping

from .ai_assessment_contract import (
    AI_REDLINE_NO_CHANGE,
    apply_span,
    clause_proposed_edits,
)
from .checks.common import (
    ISSUE_TYPE_MISSING,
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ISSUE_TYPE_UNCLEAR,
    ClauseResult,
    Paragraph,
    RedlineEdit,
    _approved_laws,
    _clause_template_text,
    _governing_law_phrase,
    _max_term_years,
    _year_count_label,
)
from .checks.signatures import SIGNATURE_FOR_LINE_PATTERN
from .inline_diff import diff_text_operation_dicts
from .redline_actions import (
    REDLINE_ACTION_LABELS,
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

RedlineBuildFn = Callable[[ClauseResult, Dict[str, Paragraph], int], List[RedlineEdit]]
SIGNATURE_MARKER_LINE_PATTERN = r"^\s*(?:by|title|date)\s*:"

# Category-A A6-05 length/count caps. An AI edit may only carry so many cuts per
# clause and so large an anchor/replacement before it is treated as runaway and
# dropped (degrade-safe). These bound the document text we are willing to splice
# in from a single model response so a malformed/adversarial edit cannot smuggle a
# multi-MB replacement (or hundreds of cuts) into one paragraph.
CATA_MAX_EDITS_PER_CLAUSE = 32
CATA_MAX_ANCHOR_QUOTE_CHARS = 4096  # a few KB: an anchor is a sentence-level span
CATA_MAX_REPLACEMENT_CHARS = 65536  # ~64KB: a paragraph-sized replacement, no more
MISSING_INSERTION_ANCHOR_PATTERNS_BY_CLAUSE = {
    "confidential_information": (
        r"\b(?:each|both|either)\s+part(?:y|ies)\b",
        r"\b(?:disclosing|receiving)\s+part(?:y|ies)\b",
        r"\bmutual(?:ly)?\b",
    ),
    "term_and_survival": (
        r"\bconfidential information\b",
        r"\bconfidentiality\b",
        r"\b(?:does not include|shall not include|exclusions?)\b",
    ),
    "governing_law": (
        r"\b(?:term|surviv(?:e|es|ed|ing|al)|expir(?:e|es|y|ation)|terminat(?:e|es|ed|ion))\b",
        r"\bconfidentiality obligations?\b",
    ),
}
FOLLOWING_INSERTION_ANCHOR_PATTERNS_BY_CLAUSE = {
    "term_and_survival": (
        r"\bgoverning\s+law\b",
        r"\bgoverned\b.{0,120}?\blaws?\s+of\b",
    ),
}


def build_redline_edits(clause_results: List[ClauseResult], paragraphs: List[Paragraph]) -> List[RedlineEdit]:
    paragraphs_by_id = {str(paragraph["id"]): paragraph for paragraph in paragraphs}
    edits: List[RedlineEdit] = []

    for clause in clause_results:
        edits.extend(redline_edits_for_clause(clause, paragraphs_by_id, len(edits) + 1))

    return edits


def redline_edits_for_clause(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    # Native clauses have a registered builder; dynamic clause types are redlined
    # generically from their own fallback wording so no per-clause Python is needed.
    builder = REDLINE_BUILDERS_BY_ID.get(str(clause["id"]))

    # Category A: honor the AI's per-span edit list. The deterministic (non-AI) path
    # carries no ``proposed_edits``, so this is inert there and the engine stays
    # byte-identical. For DYNAMIC clauses the AI honoring happens inside
    # ``_dynamic_clause_redlines`` (force-delete kept as last resort). For NATIVE
    # clauses we only let the AI edits PREEMPT the registered builder when they are
    # SURGICAL SPAN edits — something the native template builder cannot express —
    # so a plain whole-paragraph replace still flows through the native builder and
    # keeps its richer enrichment (e.g. governing_law's approved-law template
    # options). A failure inside is swallowed (fail-safe); we fall through.
    if builder is not None and _clause_has_span_edits(clause):
        ai_edits = _ai_first_redline_edits(clause, paragraphs_by_id, start_number)
        if ai_edits:
            return ai_edits

    if builder is None:
        return _dynamic_clause_redlines(clause, paragraphs_by_id, start_number)
    return builder(clause, paragraphs_by_id, start_number)


def redline_builder_ids() -> List[str]:
    return [clause_id for clause_id, _builder in REDLINE_BUILDERS]


def _dynamic_clause_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    """Build redline edits for a dynamic clause.

    Category A ordering (degrade-safe last resort preserved):

    1. HONOR the AI's per-span edit list first. For the prohibited catch-all
       (``non_circumvention``) the AI authors a surgical strike of the offending
       restraint; if that builds we use it INSTEAD of force-deleting the whole
       paragraph and discarding the model's wording.
    2. Otherwise fall back to the existing deterministic behaviour: the
       ``delete_paragraph`` force-delete (or template replace/insert) from the
       clause's ``fallback``. This stays a degrade-safe LAST RESORT so an
       empty/malformed AI output never under-redlines a real prohibited clause.
    """
    built = _redlines_from_ai_edits(clause, paragraphs_by_id, start_number)
    if built:
        return built

    fallback = clause.get("fallback")
    fallback = fallback if isinstance(fallback, dict) else {}
    action = str(fallback.get("redline_action") or "").strip()
    wording = str(fallback.get("wording") or "").strip()

    if action == REDLINE_DELETE_PARAGRAPH:
        if not _is_present_but_wrong_check(clause):
            return []
        edits: List[RedlineEdit] = []
        for paragraph in _matched_redline_paragraphs(clause, paragraphs_by_id):
            edits.append(_redline_edit(start_number + len(edits), clause, paragraph, REDLINE_DELETE_PARAGRAPH))
        return edits

    if action == REDLINE_REPLACE_PARAGRAPH and wording:
        if not _is_present_but_wrong_check(clause):
            return []
        paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
        if not paragraphs:
            return []
        # A clause authored across several <w:p> (common in PDF-converted DOCX; the
        # clause-fragment merge expands matched_paragraph_ids across EVERY fragment)
        # must be replaced as a whole: the new wording is the entire clause, so the
        # head fragment carries the replacement and every continuation fragment is
        # DELETED. Replacing only the head would leave the tail limbs of the old
        # clause dangling after the new text in the outbound redline sent to the
        # counterparty. Mirrors the delete_paragraph path's all-fragment coverage.
        edits: List[RedlineEdit] = [
            _redline_edit(start_number, clause, paragraphs[0], REDLINE_REPLACE_PARAGRAPH, replacement_text=wording)
        ]
        for tail_paragraph in paragraphs[1:]:
            edits.append(
                _redline_edit(start_number + len(edits), clause, tail_paragraph, REDLINE_DELETE_PARAGRAPH)
            )
        return edits

    if action == REDLINE_INSERT_AFTER_PARAGRAPH and wording:
        edit = _template_redline_for_required_clause(clause, paragraphs_by_id, start_number, wording)
        return [edit] if edit else []

    return []


def _clause_has_span_edits(clause: ClauseResult) -> bool:
    """True when the clause carries at least one lowered SPAN edit.

    A span edit is a sentence-level strike/replace the contract lowered to a
    paragraph replace but tagged with ``span_action``. Only these preempt a native
    clause's registered builder; a plain whole-paragraph replace defers to it.
    """
    for edit in clause_proposed_edits(clause):
        if isinstance(edit, Mapping) and str(edit.get("span_action") or "").strip():
            return True
    return False


def _ai_first_redline_edits(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    """Shared AI-honor pre-check consulted by ``redline_edits_for_clause``.

    Returns AI-authored redline edits when the clause carries a usable
    ``proposed_edits`` list (native or dynamic clause), else ``[]`` so the caller
    falls through to the deterministic builder. Fail-safe: any error is swallowed
    and treated as "no AI edits" — never raises into the board poll.
    """
    try:
        return _redlines_from_ai_edits(clause, paragraphs_by_id, start_number)
    except Exception:  # pragma: no cover - fail-safe guard, board poll must not crash
        return []


def _redlines_from_ai_edits(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    """Map a clause's AI ``proposed_edits`` to concrete redline edits.

    - Reads the v2/v3 compat edit list via ``clause_proposed_edits``.
    - SKIPS no_change edits and edits whose paragraph is absent (those degraded
      upstream in the contract; here we just drop them).
    - COALESCES multiple edits resolving to the SAME paragraph into ONE
      ``replace_paragraph`` whose replacement composes every span cut left-to-right,
      so the export never double-rebuilds one ``<w:p>`` and the coverage gate's 1:1
      paragraph matching stays valid.
    - Returns ``[]`` when no edit is usable, so the caller's force-delete/template
      fallback runs (degrade-safe). NEVER raises: a malformed edit is dropped with a
      telemetry note rather than aborting the build.
    """
    edits = clause_proposed_edits(clause)
    if not edits:
        return []
    # A6-05 count cap: refuse a runaway edit list outright (degrade-safe). The cap
    # is applied to the raw list before any per-edit work so a flood cannot cost us.
    if len(edits) > CATA_MAX_EDITS_PER_CLAUSE:
        _note_dropped_edit(
            clause, f"edit list exceeds {CATA_MAX_EDITS_PER_CLAUSE} edits ({len(edits)}); all dropped"
        )
        return []
    # A7-03 clause-ownership bound: an edit may ONLY target a paragraph this clause
    # itself matched/cited. Computed once; the empty set (fail-safe) drops every edit
    # rather than widening to the whole document.
    owned_paragraph_ids = _clause_owned_paragraph_ids(clause)

    # Group usable edits by target paragraph, preserving first-seen paragraph order.
    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    insert_after_edits: List[Dict[str, Any]] = []
    for edit in edits:
        if not isinstance(edit, Mapping):
            _note_dropped_edit(clause, "edit is not an object")
            continue
        action = str(edit.get("action") or "").strip()
        if not action or action == AI_REDLINE_NO_CHANGE:
            continue
        # A6-06: a present-but-non-string text/anchor field is dropped, never
        # str()-coerced into a Python repr inside the document.
        nonstring_reason = _edit_has_nonstring_text(edit)
        if nonstring_reason is not None:
            _note_dropped_edit(clause, f"{action} edit dropped: {nonstring_reason}")
            continue
        # A6-05: an over-long anchor/replacement is dropped (degrade), never spliced.
        length_reason = _edit_exceeds_length_caps(edit)
        if length_reason is not None:
            _note_dropped_edit(clause, f"{action} edit dropped: {length_reason}")
            continue
        paragraph_id = str(edit.get("paragraph_id") or "").strip()
        if not paragraph_id:
            _note_dropped_edit(clause, f"{action} edit has no paragraph_id")
            continue
        # A7-03: drop any edit whose target is NOT one of the clause's OWN matched
        # paragraphs — a non_circumvention edit cannot reach the signature block or
        # the governing-law line. Enforced before the existence check so a smuggled
        # (but real) paragraph_id is still rejected.
        if paragraph_id not in owned_paragraph_ids:
            _note_dropped_edit(
                clause,
                f"{action} edit targets paragraph {paragraph_id} outside the clause's matched set; dropped",
            )
            continue
        paragraph = paragraphs_by_id.get(paragraph_id)
        if paragraph is None:
            _note_dropped_edit(clause, f"{action} edit targets missing paragraph {paragraph_id}")
            continue
        if action == REDLINE_INSERT_AFTER_PARAGRAPH:
            # Inserts target a position, not a paragraph rebuild; they never collide
            # on a single ``<w:p>``, so they are kept as distinct edits.
            insert_after_edits.append({"paragraph": paragraph, "edit": edit})
            continue
        if action not in {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH}:
            _note_dropped_edit(clause, f"unsupported edit action {action}")
            continue
        if paragraph_id not in grouped:
            grouped[paragraph_id] = {"paragraph": paragraph, "edits": []}
            order.append(paragraph_id)
        grouped[paragraph_id]["edits"].append({"action": action, "edit": edit})

    built: List[RedlineEdit] = []
    for paragraph_id in order:
        bucket = grouped[paragraph_id]
        coalesced = _coalesce_paragraph_edits(
            clause,
            bucket["paragraph"],
            bucket["edits"],
            start_number + len(built),
        )
        if coalesced is not None:
            built.append(coalesced)
    for entry in insert_after_edits:
        replacement_text = _edit_replacement_text(entry["edit"])
        if not replacement_text:
            _note_dropped_edit(clause, "insert_after edit has no replacement text")
            continue
        built.append(
            _redline_edit(
                start_number + len(built),
                clause,
                entry["paragraph"],
                REDLINE_INSERT_AFTER_PARAGRAPH,
                insert_text=replacement_text,
            )
        )
    return built


def _coalesce_paragraph_edits(
    clause: ClauseResult,
    paragraph: Paragraph,
    paragraph_edits: List[Dict[str, Any]],
    edit_number: int,
) -> RedlineEdit | None:
    """Coalesce all edits on one paragraph into a single redline edit.

    A lone ``delete_paragraph`` stays a delete. Otherwise the edits compose into ONE
    ``replace_paragraph``. SPAN edits (which the contract lowered to a full-paragraph
    replacement but tagged with their original anchor) are re-applied cut-by-cut onto
    the ORIGINAL paragraph text left-to-right, so two spans on the same paragraph
    BOTH land instead of the second full replacement clobbering the first. A
    non-span replace_paragraph (whole-paragraph rewrite) takes its replacement
    verbatim. Returns ``None`` when nothing usable composes.
    """
    # A single whole-paragraph delete is expressed as a delete action (the export
    # and coverage gate treat it as a removed paragraph), matching legacy behaviour.
    if len(paragraph_edits) == 1 and paragraph_edits[0]["action"] == REDLINE_DELETE_PARAGRAPH:
        return _redline_edit(edit_number, clause, paragraph, REDLINE_DELETE_PARAGRAPH)

    original_text = str(paragraph["text"])
    new_text = original_text
    applied_any = False
    for entry in paragraph_edits:
        action = entry["action"]
        edit = entry["edit"]
        if action == REDLINE_DELETE_PARAGRAPH:
            # A delete coalesced with other edits empties the paragraph.
            new_text = ""
            applied_any = True
            continue
        span_anchor = str(edit.get("span_anchor_quote") or "").strip()
        if edit.get("span_action") and span_anchor:
            # Compose this span's cut onto the running text (NOT the original full
            # replacement, which only reflected this one span). apply_span returns
            # None if the anchor moved out of range after a prior cut — drop just
            # this span rather than clobbering the paragraph.
            replacement = str(edit.get("span_replacement") or "")
            composed = apply_span(new_text, span_anchor, replacement)
            if composed is None:
                _note_dropped_edit(clause, f"span anchor not found during coalesce: {span_anchor[:40]}")
                continue
            new_text = composed
            applied_any = True
            continue
        # A non-span whole-paragraph replace takes its replacement verbatim.
        replacement_text = _edit_replacement_text(edit)
        if not replacement_text:
            _note_dropped_edit(clause, "replace edit has no replacement text")
            continue
        new_text = replacement_text
        applied_any = True

    if not applied_any:
        return None
    if new_text == original_text:
        # Composition produced no change; nothing to redline.
        return None
    return _redline_edit(
        edit_number,
        clause,
        paragraph,
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=new_text,
    )


def _edit_replacement_text(edit: Mapping[str, Any]) -> str:
    """Resolve an edit's replacement text from ``replacement`` or legacy ``text``."""
    value = edit.get("replacement")
    if value is None:
        value = edit.get("text")
    return str(value or "").strip()


def _clause_owned_paragraph_ids(clause: ClauseResult) -> set[str]:
    """The set of paragraph ids this clause may legitimately redline.

    Category-A A7-03 (cross-clause mutation defense): an AI edit may only target a
    paragraph the clause itself LOCATED/CITED — i.e. one of its
    ``matched_paragraph_ids`` (the evidence matcher's output). It must NOT be able
    to rewrite an unrelated, sensitive paragraph (the signature block, the
    governing-law line) just because that paragraph exists in the global document.

    ``redline_target_paragraph_ids`` is intentionally NOT consulted here: that field
    is the OVER-BROAD union of every edit's own ``paragraph_id`` (including the
    smuggled cross-clause target), so trusting it would let the attack bootstrap its
    own authorization. The matched set is the evidence-grounded source of truth.

    Fail-SAFE: if the matched set is empty/unavailable, return the empty set so the
    caller DROPS every edit (never widen to the whole document). NEVER raises.
    """
    try:
        paragraph_ids = clause.get("matched_paragraph_ids", []) if isinstance(clause, Mapping) else []
        if not isinstance(paragraph_ids, list):
            return set()
        return {str(paragraph_id).strip() for paragraph_id in paragraph_ids if str(paragraph_id).strip()}
    except Exception:  # pragma: no cover - fail-safe guard, drop rather than widen
        return set()


def _edit_exceeds_length_caps(edit: Mapping[str, Any]) -> str | None:
    """A6-05: reason string if an edit's anchor/replacement exceeds the byte caps.

    Returns a short telemetry reason when the edit's ``anchor_quote`` (or its lowered
    ``span_anchor_quote``) or its replacement text (``replacement``/``text``/
    ``span_replacement``) is over-long, else ``None``. Length is measured on the raw
    string value so a multi-MB blob is caught BEFORE it is spliced into a paragraph.
    """
    anchor_value = edit.get("anchor_quote")
    if anchor_value is None:
        anchor_value = edit.get("span_anchor_quote")
    if isinstance(anchor_value, str) and len(anchor_value) > CATA_MAX_ANCHOR_QUOTE_CHARS:
        return f"anchor_quote exceeds {CATA_MAX_ANCHOR_QUOTE_CHARS} chars"
    for key in ("replacement", "text", "span_replacement"):
        value = edit.get(key)
        if isinstance(value, str) and len(value) > CATA_MAX_REPLACEMENT_CHARS:
            return f"{key} exceeds {CATA_MAX_REPLACEMENT_CHARS} chars"
    return None


def _edit_has_nonstring_text(edit: Mapping[str, Any]) -> str | None:
    """A6-06: reason string if a text field is present but NOT a string.

    A dict/list/int where a string is expected must DROP the edit, never be
    ``str()``-coerced — coercing would splice a Python repr (``{'x': 1}``) into the
    rendered document as if it were redline text. A genuinely absent field (None /
    missing) is fine; only a present non-string value is rejected.
    """
    for key in ("anchor_quote", "span_anchor_quote", "replacement", "text", "span_replacement"):
        if key in edit:
            value = edit.get(key)
            if value is not None and not isinstance(value, str):
                return f"{key} is not a string ({type(value).__name__})"
    return None


def _note_dropped_edit(clause: ClauseResult, reason: str) -> None:
    """Record a dropped-edit telemetry note on the clause, fail-safe.

    Appends to ``clause["catA_dropped_edits"]`` so the audit trail shows which AI
    edits were unusable, without raising. The board poll never depends on this.
    """
    try:
        if not isinstance(clause, dict):
            return
        notes = clause.setdefault("catA_dropped_edits", [])
        if isinstance(notes, list):
            notes.append(str(reason))
    except Exception:  # pragma: no cover - telemetry must never crash the build
        pass


def _is_present_but_wrong_check(clause: ClauseResult) -> bool:
    return clause.get("status") == "check" and clause.get("issue_type") == ISSUE_TYPE_PRESENT_BUT_WRONG


def _is_missing_required_check(clause: ClauseResult) -> bool:
    return (
        clause.get("status") == "not_present"
        and clause.get("issue_type") == ISSUE_TYPE_MISSING
        and not clause.get("passes")
    )


def _matched_redline_paragraphs(clause: ClauseResult, paragraphs_by_id: Dict[str, Paragraph]) -> List[Paragraph]:
    paragraph_ids = clause.get("matched_paragraph_ids", [])
    if not isinstance(paragraph_ids, list):
        return []
    return [
        paragraph
        for paragraph_id in paragraph_ids
        if (paragraph := paragraphs_by_id.get(str(paragraph_id))) is not None
    ]


def _insertion_anchor_paragraph(clause: ClauseResult, paragraphs_by_id: Dict[str, Paragraph]) -> Paragraph | None:
    matched_paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if matched_paragraphs:
        return matched_paragraphs[-1]
    paragraphs = _ordered_paragraphs(paragraphs_by_id)
    if not paragraphs:
        return None
    if str(clause.get("id")) == "signatures":
        return paragraphs[-1]

    anchor = _logical_missing_clause_anchor(clause, paragraphs)
    if anchor:
        return anchor
    return _last_non_signature_paragraph(paragraphs) or paragraphs[-1]


def _logical_missing_clause_anchor(clause: ClauseResult, paragraphs: List[Paragraph]) -> Paragraph | None:
    clause_id = str(clause.get("id"))
    if clause_id == "mutuality":
        return _first_non_signature_paragraph(paragraphs)

    anchor = _last_matching_non_signature_paragraph(
        paragraphs,
        MISSING_INSERTION_ANCHOR_PATTERNS_BY_CLAUSE.get(clause_id, ()),
    )
    if anchor:
        return anchor

    anchor = _paragraph_before_first_matching(
        paragraphs,
        FOLLOWING_INSERTION_ANCHOR_PATTERNS_BY_CLAUSE.get(clause_id, ()),
    )
    if anchor:
        return anchor

    return _paragraph_before_first_signature_block(paragraphs)


def _ordered_paragraphs(paragraphs_by_id: Dict[str, Paragraph]) -> List[Paragraph]:
    return sorted(paragraphs_by_id.values(), key=lambda paragraph: int(paragraph.get("index", 0)))


def _first_non_signature_paragraph(paragraphs: List[Paragraph]) -> Paragraph | None:
    return next((paragraph for paragraph in paragraphs if not _is_signature_anchor_paragraph(paragraph)), None)


def _last_non_signature_paragraph(paragraphs: List[Paragraph]) -> Paragraph | None:
    return next((paragraph for paragraph in reversed(paragraphs) if not _is_signature_anchor_paragraph(paragraph)), None)


def _last_matching_non_signature_paragraph(paragraphs: List[Paragraph], patterns: tuple[str, ...]) -> Paragraph | None:
    if not patterns:
        return None
    return next(
        (
            paragraph
            for paragraph in reversed(paragraphs)
            if not _is_signature_anchor_paragraph(paragraph)
            and any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for pattern in patterns)
        ),
        None,
    )


def _paragraph_before_first_matching(paragraphs: List[Paragraph], patterns: tuple[str, ...]) -> Paragraph | None:
    if not patterns:
        return None
    previous: Paragraph | None = None
    for paragraph in paragraphs:
        if any(re.search(pattern, str(paragraph["text"]), flags=re.IGNORECASE) for pattern in patterns):
            if previous and not _is_signature_anchor_paragraph(previous):
                return previous
            return None
        if not _is_signature_anchor_paragraph(paragraph):
            previous = paragraph
    return None


def _paragraph_before_first_signature_block(paragraphs: List[Paragraph]) -> Paragraph | None:
    previous: Paragraph | None = None
    for paragraph in paragraphs:
        if _is_signature_anchor_paragraph(paragraph):
            return previous
        previous = paragraph
    return None


def _is_signature_anchor_paragraph(paragraph: Paragraph) -> bool:
    text = str(paragraph["text"])
    marker_count = len(re.findall(SIGNATURE_MARKER_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE))
    has_for_line = bool(re.search(SIGNATURE_FOR_LINE_PATTERN, text, flags=re.IGNORECASE | re.MULTILINE))
    return marker_count >= 2 or (has_for_line and marker_count >= 1)


def _redline_edit(
    edit_number: int,
    clause: ClauseResult,
    paragraph: Paragraph,
    action: str,
    replacement_text: str = "",
    insert_text: str = "",
    template_options: List[Dict[str, object]] | None = None,
) -> RedlineEdit:
    proposed_text = insert_text or replacement_text
    edit = {
        "id": f"r{edit_number}",
        "clause_id": clause["id"],
        "clause_name": clause["name"],
        "paragraph_id": paragraph["id"],
        "paragraph_index": paragraph.get("index"),
        "action": action,
        "action_label": REDLINE_ACTION_LABELS.get(action, "Proposed edit"),
        "status": "proposed",
        "original_text": "" if action == REDLINE_INSERT_AFTER_PARAGRAPH else paragraph["text"],
        "replacement_text": proposed_text,
        "reason": clause.get("what_to_fix") or clause.get("reason"),
    }
    if "source_index" in paragraph:
        edit["source_index"] = paragraph["source_index"]
    if "source_part" in paragraph:
        edit["source_part"] = paragraph["source_part"]
    if action == REDLINE_INSERT_AFTER_PARAGRAPH:
        edit["anchor_text"] = paragraph["text"]
        edit["insert_text"] = proposed_text
    elif action == REDLINE_REPLACE_PARAGRAPH:
        edit["inline_diff_operations"] = diff_text_operation_dicts(str(paragraph["text"]), proposed_text)
    if template_options:
        edit["template_options"] = _redline_template_options_with_diff(paragraph, action, template_options)
    return edit


def _redline_template_options_with_diff(
    paragraph: Paragraph,
    action: str,
    template_options: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    if action != REDLINE_REPLACE_PARAGRAPH:
        return template_options
    return [
        {
            **option,
            "inline_diff_operations": diff_text_operation_dicts(
                str(paragraph["text"]),
                str(option.get("replacement_text") or option.get("text") or ""),
            ),
        }
        for option in template_options
    ]


def _governing_law_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    template_options = _governing_law_template_options(clause)
    if not template_options:
        return None

    selected_template = _selected_template_option(template_options)

    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None
        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=str(selected_template["text"]),
            template_options=template_options,
        )

    if not _is_present_but_wrong_check(clause):
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=str(selected_template["text"]),
        template_options=template_options,
    )


def _governing_law_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _governing_law_redline(clause, paragraphs_by_id, start_number)
    return [edit] if edit else []


def _template_redline_for_required_clause(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
    template_text: str,
) -> RedlineEdit | None:
    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None
        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=template_text,
        )

    if not _is_present_but_wrong_check(clause):
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=template_text,
    )


def _mutuality_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _template_redline_for_required_clause(
        clause,
        paragraphs_by_id,
        start_number,
        _clause_template_text(clause, "redline_template"),
    )
    return [edit] if edit else []


def _confidential_information_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    template_field = "standard_exclusions_template" if _confidential_issue_is_exclusion_based(clause) else "redline_template"
    edit = _template_redline_for_required_clause(
        clause,
        paragraphs_by_id,
        start_number,
        _clause_template_text(clause, template_field),
    )
    return [edit] if edit else []


def _confidential_issue_is_exclusion_based(clause: ClauseResult) -> bool:
    issue_text = f"{clause.get('reason', '')} {clause.get('what_to_fix', '')}".lower()
    return "exclusion" in issue_text or "residual" in issue_text or "reverse-engineering" in issue_text


def _term_and_survival_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None
        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=_term_and_survival_replacement_text(clause),
        )

    if clause.get("status") == "check" and clause.get("issue_type") == ISSUE_TYPE_UNCLEAR:
        paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
        if not paragraphs:
            return None
        return _redline_edit(
            edit_number,
            clause,
            paragraphs[0],
            REDLINE_REPLACE_PARAGRAPH,
            replacement_text=_term_and_survival_replacement_text(clause),
        )

    if not _is_present_but_wrong_check(clause):
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=_term_and_survival_replacement_text(clause),
    )


def _term_and_survival_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _term_and_survival_redline(clause, paragraphs_by_id, start_number)
    return [edit] if edit else []


def _signatures_redline(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    edit_number: int,
) -> RedlineEdit | None:
    if _is_missing_required_check(clause):
        anchor = _insertion_anchor_paragraph(clause, paragraphs_by_id)
        if not anchor:
            return None

        return _redline_edit(
            edit_number,
            clause,
            anchor,
            REDLINE_INSERT_AFTER_PARAGRAPH,
            insert_text=_signature_block_template(clause),
        )

    if clause.get("status") != "check" or clause.get("issue_type") != ISSUE_TYPE_UNCLEAR:
        return None

    paragraphs = _matched_redline_paragraphs(clause, paragraphs_by_id)
    if not paragraphs:
        return None

    return _redline_edit(
        edit_number,
        clause,
        paragraphs[0],
        REDLINE_REPLACE_PARAGRAPH,
        replacement_text=_signature_block_template(clause),
    )


def _signatures_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    edit = _signatures_redline(clause, paragraphs_by_id, start_number)
    if not edit:
        return []
    edits = [edit]
    if edit["action"] != REDLINE_REPLACE_PARAGRAPH:
        return edits
    for paragraph in _matched_redline_paragraphs(clause, paragraphs_by_id)[1:]:
        edits.append(
            _redline_edit(
                start_number + len(edits),
                clause,
                paragraph,
                REDLINE_DELETE_PARAGRAPH,
            )
        )
    return edits


REDLINE_BUILDERS: List[tuple[str, RedlineBuildFn]] = [
    ("mutuality", _mutuality_redlines),
    ("confidential_information", _confidential_information_redlines),
    ("governing_law", _governing_law_redlines),
    ("term_and_survival", _term_and_survival_redlines),
    ("signatures", _signatures_redlines),
]
REDLINE_BUILDERS_BY_ID: Dict[str, RedlineBuildFn] = dict(REDLINE_BUILDERS)


def _selected_template_option(template_options: List[Dict[str, object]]) -> Dict[str, object]:
    return next((option for option in template_options if option.get("selected")), template_options[0])


def _governing_law_template_options(clause: ClauseResult) -> List[Dict[str, object]]:
    approved_laws = _approved_laws(clause)
    preferred_law = _preferred_governing_law(clause)
    if not approved_laws:
        return []

    return [
        {
            "id": f"governing_law_{_template_slug(law)}",
            "label": law,
            "text": _governing_law_replacement_text(clause, law),
            "replacement_text": _governing_law_replacement_text(clause, law),
            "insert_text": _governing_law_replacement_text(clause, law),
            "selected": law == preferred_law,
        }
        for law in approved_laws
    ]


def _template_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _preferred_governing_law(clause: ClauseResult) -> str | None:
    approved_laws = _approved_laws(clause)
    preferred_law = str(clause.get("preferred_law", "")).strip()

    if preferred_law and (not approved_laws or preferred_law in approved_laws):
        return preferred_law
    if approved_laws:
        return approved_laws[0]
    return None


def _governing_law_replacement_text(clause: ClauseResult, law: str) -> str:
    law_label = law.strip()
    law_phrase = _governing_law_phrase(clause, law_label)
    return f"This Agreement shall be governed by the laws of {law_phrase}."


def _term_and_survival_replacement_text(clause: ClauseResult) -> str:
    cap_label = _year_count_label(_max_term_years(clause))
    return _clause_template_text(
        clause,
        "redline_template",
        {
            "max_term_years": _max_term_years(clause),
            "max_term_years_label": cap_label,
        },
    )


def _signature_block_template(clause: ClauseResult) -> str:
    return _clause_template_text(clause, "redline_template")
