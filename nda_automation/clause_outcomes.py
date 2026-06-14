from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Mapping

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
from .redline_anchor import structure_aware_insertion_anchor

# #6: the contract structure for the document currently being redlined. Set for the
# duration of build_redline_edits so _insertion_anchor_paragraph can place a MISSING
# clause by real section order, without threading the structure through every
# fixed-signature builder in REDLINE_BUILDERS. None (the default) reproduces the exact
# legacy regex-tier behaviour, so every existing caller is unaffected.
_ACTIVE_CONTRACT_STRUCTURE: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "active_contract_structure", default=None
)

RedlineBuildFn = Callable[[ClauseResult, Dict[str, Paragraph], int], List[RedlineEdit]]
SIGNATURE_MARKER_LINE_PATTERN = r"^\s*(?:by|title|date)\s*:"
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


def build_redline_edits(
    clause_results: List[ClauseResult],
    paragraphs: List[Paragraph],
    *,
    contract_structure: Mapping[str, Any] | None = None,
) -> List[RedlineEdit]:
    paragraphs_by_id = {str(paragraph["id"]): paragraph for paragraph in paragraphs}
    edits: List[RedlineEdit] = []

    # #6: expose the document structure to _insertion_anchor_paragraph for the
    # duration of this build only. Reset in finally so the contextvar never leaks to
    # an unrelated later build (and so a nested/concurrent build sees its own value).
    token = _ACTIVE_CONTRACT_STRUCTURE.set(contract_structure)
    try:
        for clause in clause_results:
            edits.extend(redline_edits_for_clause(clause, paragraphs_by_id, len(edits) + 1))
    finally:
        _ACTIVE_CONTRACT_STRUCTURE.reset(token)

    return edits


def redline_edits_for_clause(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    # Native clauses have a registered builder; dynamic clause types are redlined
    # generically from their own fallback wording so no per-clause Python is needed.
    builder = REDLINE_BUILDERS_BY_ID.get(str(clause["id"]))
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
    """Build redline edits for a dynamic clause from its fallback wording."""
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
        return [
            _redline_edit(start_number, clause, paragraphs[0], REDLINE_REPLACE_PARAGRAPH, replacement_text=wording)
        ]

    if action == REDLINE_INSERT_AFTER_PARAGRAPH and wording:
        edit = _template_redline_for_required_clause(clause, paragraphs_by_id, start_number, wording)
        return [edit] if edit else []

    return []


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

    # #6 (guarded): prefer a structure-aware anchor that places the missing clause in
    # real section order. structure_aware_insertion_anchor returns None whenever it
    # cannot produce a confident, source-backed, pre-signature anchor, so the existing
    # regex tiers below remain the authoritative fallback and unchanged behaviour when
    # no structure is supplied.
    structure_anchor = structure_aware_insertion_anchor(
        str(clause.get("id") or ""),
        paragraphs_by_id,
        _ACTIVE_CONTRACT_STRUCTURE.get(),
    )
    if structure_anchor is not None:
        return structure_anchor

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
