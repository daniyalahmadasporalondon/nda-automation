from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List

from .redline_actions import (
    REDLINE_ACTION_LABELS,
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .inline_diff import diff_text_operation_dicts
from .checks import CLAUSE_CHECKS
from .checks.common import (
    ISSUE_TYPE_MISSING,
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ISSUE_TYPE_UNCLEAR,
    ClauseResult,
    PlaybookTemplateError,
    RedlineEdit,
    _approved_laws,
    _clause_template_text,
    _governing_law_phrase,
    _max_term_years,
    _normalize,
    _paragraph_matches,
    _year_count_label,
)
from .review_document import (
    Paragraph,
    ParagraphAlignmentError as ParagraphAlignmentError,
    align_document_paragraphs,
    split_document_paragraphs,
    validate_clause_evidence_trust,
)

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_PATH = ROOT / "playbook.json"
RedlineBuildFn = Callable[[ClauseResult, Dict[str, Paragraph], int], List[RedlineEdit]]
__all__ = [
    "ParagraphAlignmentError",
    "PlaybookTemplateError",
    "_paragraph_matches",
    "load_playbook",
    "review_nda",
    "split_document_paragraphs",
    "validate_playbook",
    "validate_clause_evidence_trust",
]


def load_playbook() -> Dict[str, object]:
    try:
        with PLAYBOOK_PATH.open("r", encoding="utf-8") as handle:
            playbook = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PlaybookTemplateError("Playbook must be valid JSON.") from exc
    if not isinstance(playbook, dict):
        raise PlaybookTemplateError("Playbook must be a JSON object.")
    return playbook


def validate_playbook(playbook: Dict[str, object]) -> None:
    _validate_playbook_contract(playbook)


def review_nda(text: str, paragraphs: List[Paragraph] | None = None) -> Dict[str, object]:
    source_text = text or ""
    if paragraphs is None:
        document_paragraphs = split_document_paragraphs(source_text)
    else:
        if not source_text:
            source_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)
        document_paragraphs = align_document_paragraphs(paragraphs, source_text)

    normalized = _normalize(source_text)
    playbook = load_playbook()
    _validate_playbook_contract(playbook)
    clauses_by_id = {clause["id"]: clause for clause in playbook["clauses"]}

    clause_results = [
        check(source_text, normalized, clauses_by_id[clause_id], document_paragraphs)
        for clause_id, check in CLAUSE_CHECKS
    ]
    failed = [clause for clause in clause_results if not clause["passes"]]
    redline_edits = _build_redline_edits(clause_results, document_paragraphs)

    result = {
        "overall_status": "does_not_meet_requirements" if failed else "meets_requirements",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "requirements_passed": len(clause_results) - len(failed),
        "requirements_failed": len(failed),
        "paragraphs": document_paragraphs,
        "clauses": clause_results,
        "redline_edits": redline_edits,
    }
    evidence_errors = validate_clause_evidence_trust(result, source_text)
    if evidence_errors:
        _flag_evidence_trust_errors(result, evidence_errors)
    else:
        result["evidence_trust"] = {"status": "verified", "errors": []}
    return result


def _flag_evidence_trust_errors(result: Dict[str, object], errors: List[str]) -> None:
    result["evidence_trust"] = {"status": "flagged", "errors": errors}
    result["review_warnings"] = [
        {
            "type": "evidence_provenance_drift",
            "message": "Clause evidence provenance drift detected.",
            "details": errors,
        }
    ]


def _validate_check_registry() -> None:
    check_ids = [clause_id for clause_id, _check in CLAUSE_CHECKS]
    duplicate_check_ids = sorted({clause_id for clause_id in check_ids if check_ids.count(clause_id) > 1})
    if duplicate_check_ids:
        raise RuntimeError(f"Duplicate checker IDs: {', '.join(duplicate_check_ids)}")

    playbook_clauses = load_playbook()["clauses"]
    playbook_ids = [str(clause["id"]) for clause in playbook_clauses]
    duplicate_playbook_ids = sorted({clause_id for clause_id in playbook_ids if playbook_ids.count(clause_id) > 1})
    if duplicate_playbook_ids:
        raise RuntimeError(f"Duplicate playbook IDs: {', '.join(duplicate_playbook_ids)}")

    missing_search_terms = [
        str(clause["id"])
        for clause in playbook_clauses
        if not _required_clause_terms(clause, "search_terms")
    ]
    if missing_search_terms:
        raise RuntimeError(f"Playbook clauses missing search_terms: {', '.join(missing_search_terms)}")

    missing_checks = sorted(set(playbook_ids) - set(check_ids))
    extra_checks = sorted(set(check_ids) - set(playbook_ids))
    if missing_checks or extra_checks:
        detail = []
        if missing_checks:
            detail.append(f"missing checks for: {', '.join(missing_checks)}")
        if extra_checks:
            detail.append(f"checks without playbook clauses: {', '.join(extra_checks)}")
        raise RuntimeError("Checker registry does not match playbook (" + "; ".join(detail) + ")")

    builder_ids = [clause_id for clause_id, _builder in REDLINE_BUILDERS]
    duplicate_builder_ids = sorted({clause_id for clause_id in builder_ids if builder_ids.count(clause_id) > 1})
    if duplicate_builder_ids:
        raise RuntimeError(f"Duplicate redline builder IDs: {', '.join(duplicate_builder_ids)}")

    if builder_ids != check_ids:
        missing_builders = sorted(set(check_ids) - set(builder_ids))
        extra_builders = sorted(set(builder_ids) - set(check_ids))
        detail = []
        if missing_builders:
            detail.append(f"missing redline builders for: {', '.join(missing_builders)}")
        if extra_builders:
            detail.append(f"redline builders without checks: {', '.join(extra_builders)}")
        if not detail:
            detail.append("redline builder order differs from checker order")
        raise RuntimeError("Redline registry does not mirror checker registry (" + "; ".join(detail) + ")")


def _validate_playbook_contract(playbook: Dict[str, object]) -> None:
    clauses = playbook.get("clauses")
    if not isinstance(clauses, list):
        raise PlaybookTemplateError("Playbook clauses must be a list.")

    playbook_ids = []
    for clause in clauses:
        if not isinstance(clause, dict):
            raise PlaybookTemplateError("Each playbook clause must be an object.")
        clause_id = str(clause.get("id", "")).strip()
        if not clause_id:
            raise PlaybookTemplateError("Each playbook clause must include an id.")
        playbook_ids.append(clause_id)
        for field in ["name", "requirement", "type"]:
            if not isinstance(clause.get(field), str) or not str(clause.get(field)).strip():
                raise PlaybookTemplateError(f"Playbook clause {clause_id} must include {field}.")
        if clause["type"] not in {"required", "prohibited"}:
            raise PlaybookTemplateError(f"Playbook clause {clause_id} has invalid type.")
        if not _required_clause_terms(clause, "search_terms"):
            raise PlaybookTemplateError(f"Playbook clause {clause_id} must include search_terms.")
        for optional_list_field in ["taxonomy_groups", "semantic_signals"]:
            if optional_list_field in clause and not isinstance(clause[optional_list_field], list):
                raise PlaybookTemplateError(f"Playbook clause {clause_id} {optional_list_field} must be a list.")
        for optional_text_field in ["rationale", "evidence_guidance"]:
            if optional_text_field in clause and not isinstance(clause[optional_text_field], str):
                raise PlaybookTemplateError(f"Playbook clause {clause_id} {optional_text_field} must be text.")

    duplicate_ids = sorted({clause_id for clause_id in playbook_ids if playbook_ids.count(clause_id) > 1})
    if duplicate_ids:
        raise PlaybookTemplateError(f"Duplicate playbook IDs: {', '.join(duplicate_ids)}")

    check_ids = [clause_id for clause_id, _check in CLAUSE_CHECKS]
    missing_playbook_ids = sorted(set(check_ids) - set(playbook_ids))
    extra_playbook_ids = sorted(set(playbook_ids) - set(check_ids))
    if missing_playbook_ids or extra_playbook_ids:
        detail = []
        if missing_playbook_ids:
            detail.append(f"missing clauses: {', '.join(missing_playbook_ids)}")
        if extra_playbook_ids:
            detail.append(f"unknown clauses: {', '.join(extra_playbook_ids)}")
        raise PlaybookTemplateError("Playbook clause IDs do not match checker IDs (" + "; ".join(detail) + ")")

    clauses_by_id = {str(clause["id"]): clause for clause in clauses}
    _validate_governing_law_playbook(clauses_by_id["governing_law"])
    _require_template(clauses_by_id["mutuality"], "redline_template")
    _require_template(clauses_by_id["confidential_information"], "redline_template")
    _require_template(clauses_by_id["confidential_information"], "standard_exclusions_template")
    _require_template(clauses_by_id["term_and_survival"], "redline_template")
    _require_template(clauses_by_id["signatures"], "redline_template")


def _validate_governing_law_playbook(clause: Dict[str, object]) -> None:
    approved_laws = _approved_laws(clause)
    if not approved_laws:
        raise PlaybookTemplateError("Playbook clause governing_law must include approved_laws.")
    preferred_law = str(clause.get("preferred_law", "")).strip()
    if preferred_law and preferred_law not in approved_laws:
        raise PlaybookTemplateError("Playbook clause governing_law preferred_law must be approved.")
    law_phrases = clause.get("law_phrases", {})
    if not isinstance(law_phrases, dict):
        raise PlaybookTemplateError("Playbook clause governing_law law_phrases must be an object.")
    missing_phrases = [law for law in approved_laws if not str(law_phrases.get(law, "")).strip()]
    if missing_phrases:
        raise PlaybookTemplateError(
            "Playbook clause governing_law law_phrases missing: " + ", ".join(missing_phrases)
        )


def _require_template(clause: Dict[str, object], field: str) -> None:
    clause_id = str(clause.get("id", "unknown"))
    if not isinstance(clause.get(field), str) or not str(clause.get(field)).strip():
        raise PlaybookTemplateError(f"Playbook clause {clause_id} must include {field}.")


def _required_clause_terms(clause: Dict[str, object], field: str) -> List[str]:
    values = clause.get(field, [])
    if not isinstance(values, list):
        return []
    return [str(term).lower().strip() for term in values if str(term).strip()]


def _build_redline_edits(clause_results: List[ClauseResult], paragraphs: List[Paragraph]) -> List[RedlineEdit]:
    paragraphs_by_id = {str(paragraph["id"]): paragraph for paragraph in paragraphs}
    edits: List[RedlineEdit] = []

    for clause in clause_results:
        edits.extend(_redline_edits_for_clause(clause, paragraphs_by_id, len(edits) + 1))

    return edits


def _redline_edits_for_clause(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    builder = REDLINE_BUILDERS_BY_ID[str(clause["id"])]
    return builder(clause, paragraphs_by_id, start_number)


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
    if not paragraphs_by_id:
        return None
    return max(paragraphs_by_id.values(), key=lambda paragraph: int(paragraph.get("index", 0)))


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


def _non_circumvention_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    if not _is_present_but_wrong_check(clause):
        return []

    edits: List[RedlineEdit] = []
    for paragraph in _matched_redline_paragraphs(clause, paragraphs_by_id):
        edits.append(
            _redline_edit(
                start_number + len(edits),
                clause,
                paragraph,
                REDLINE_DELETE_PARAGRAPH,
            )
        )
    return edits


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


def _no_redlines(
    clause: ClauseResult,
    paragraphs_by_id: Dict[str, Paragraph],
    start_number: int,
) -> List[RedlineEdit]:
    return []


REDLINE_BUILDERS: List[tuple[str, RedlineBuildFn]] = [
    # Every checked clause must declare its redline behavior. Use
    # _no_redlines when the absence of a proposed edit is intentional.
    ("mutuality", _mutuality_redlines),
    ("confidential_information", _confidential_information_redlines),
    ("governing_law", _governing_law_redlines),
    ("term_and_survival", _term_and_survival_redlines),
    ("non_circumvention", _non_circumvention_redlines),
    ("signatures", _signatures_redlines),
]
REDLINE_BUILDERS_BY_ID: Dict[str, RedlineBuildFn] = dict(REDLINE_BUILDERS)


_validate_check_registry()


def _preferred_governing_law(clause: ClauseResult) -> str | None:
    approved_laws = _approved_laws(clause)
    preferred_law = str(clause.get("preferred_law", "")).strip()

    if preferred_law and (not approved_laws or preferred_law in approved_laws):
        return preferred_law
    if approved_laws:
        return approved_laws[0]
    return None


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
