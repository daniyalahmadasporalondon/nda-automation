from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .docx_xml import _normalize_paragraph_text
from .redline_actions import (
    REDLINE_ACTION_LABELS,
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_FORMAT_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

MANUAL_VIEWER_EDIT_CLAUSE_ID = "manual_viewer_edit"

REDLINE_ACTIONS = frozenset({
    REDLINE_REPLACE_PARAGRAPH,
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_FORMAT_PARAGRAPH,
})
MANUAL_REDLINE_ACTIONS = frozenset({
    REDLINE_REPLACE_PARAGRAPH,
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_FORMAT_PARAGRAPH,
})


def is_known_redline_action(action: object) -> bool:
    return str(action or "") in REDLINE_ACTIONS


def is_manual_redline_action(action: object) -> bool:
    return str(action or "") in MANUAL_REDLINE_ACTIONS


def is_manual_redline_edit(edit: object) -> bool:
    if not isinstance(edit, dict):
        return False
    return bool(edit.get("is_manual") or edit.get("clause_id") == MANUAL_VIEWER_EDIT_CLAUSE_ID)


def is_insertion_redline_edit(edit: object) -> bool:
    return isinstance(edit, dict) and edit.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH


def is_freeform_manual_replace_edit(edit: object) -> bool:
    return (
        isinstance(edit, dict)
        and edit.get("action") == REDLINE_REPLACE_PARAGRAPH
        and is_manual_redline_edit(edit)
        and not edit.get("whole_paragraph")
    )


def redline_action_label(edit: object) -> str:
    if isinstance(edit, dict) and edit.get("action_label"):
        return str(edit.get("action_label"))
    action = edit.get("action") if isinstance(edit, dict) else edit
    return REDLINE_ACTION_LABELS.get(str(action or ""), "Proposed edit")


def redline_inserted_text(edit: object) -> str:
    if not isinstance(edit, dict):
        return ""
    return str(edit.get("insert_text") or edit.get("replacement_text") or "")


def redline_replacement_text(edit: object) -> str:
    if not isinstance(edit, dict):
        return ""
    if edit.get("action") == REDLINE_DELETE_PARAGRAPH:
        return ""
    if edit.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH:
        return redline_inserted_text(edit)
    return str(edit.get("replacement_text") or "")


def has_inline_diff_operations(edit: object) -> bool:
    return (
        isinstance(edit, dict)
        and isinstance(edit.get("inline_diff_operations"), list)
        and len(edit["inline_diff_operations"]) > 0
    )


def redline_inline_preview_mode(edit: object) -> str:
    if has_inline_diff_operations(edit):
        return "operations"
    if isinstance(edit, dict) and edit.get("whole_paragraph"):
        return "whole_paragraph"
    if is_freeform_manual_replace_edit(edit):
        return "character_diff"
    return "whole_paragraph"


def redline_operation_preview_mode(edit: object) -> str:
    if has_inline_diff_operations(edit):
        return "operations"
    if isinstance(edit, dict) and edit.get("whole_paragraph"):
        return "whole_paragraph"
    if is_freeform_manual_replace_edit(edit):
        return "word_diff"
    return "whole_paragraph"


def normalize_redline_edit(
    raw: object,
    *,
    allowed_actions: set[str] | frozenset[str] | None = None,
    manual: bool | None = None,
    require_paragraph: bool = True,
    require_content: bool = False,
    trim_text: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "")
    if action not in REDLINE_ACTIONS:
        return None
    if allowed_actions is not None and action not in allowed_actions:
        return None

    manual_edit = is_manual_redline_edit(raw) if manual is None else manual
    if manual_edit and action not in MANUAL_REDLINE_ACTIONS:
        return None

    paragraph_id = str(raw.get("paragraph_id") or "").strip()
    if require_paragraph and not paragraph_id:
        return None

    original_text = _clean_text(raw.get("original_text"), trim=trim_text)
    replacement_text = _clean_text(raw.get("replacement_text"), trim=trim_text)
    anchor_text = _clean_text(raw.get("anchor_text"), trim=trim_text)
    insert_text = _clean_text(raw.get("insert_text"), trim=trim_text)

    if require_content and not _has_required_redline_text(
        action,
        original_text=original_text,
        replacement_text=replacement_text,
        insert_text=insert_text,
    ):
        return None

    normalized = _copy_jsonish_dict(raw)
    normalized["action"] = action
    normalized["action_label"] = redline_action_label(normalized)
    normalized["paragraph_id"] = paragraph_id
    normalized["original_text"] = original_text
    normalized["replacement_text"] = _normalized_replacement_text(action, original_text, replacement_text)
    normalized["status"] = str(raw.get("status") or "proposed")
    if manual_edit:
        normalized["clause_id"] = MANUAL_VIEWER_EDIT_CLAUSE_ID
    elif raw.get("clause_id") is not None:
        normalized["clause_id"] = str(raw.get("clause_id") or "").strip()
    if raw.get("id") is not None:
        normalized["id"] = str(raw.get("id") or "").strip()
    if anchor_text:
        normalized["anchor_text"] = anchor_text
    elif "anchor_text" in normalized:
        normalized.pop("anchor_text", None)
    if insert_text:
        normalized["insert_text"] = insert_text
    elif action != REDLINE_INSERT_AFTER_PARAGRAPH:
        normalized.pop("insert_text", None)
    for key in ("paragraph_index", "source_index"):
        if key in normalized:
            index = _clean_index(normalized.get(key))
            if index is None:
                normalized.pop(key, None)
            else:
                normalized[key] = index
    if raw.get("source_part") is not None:
        source_part = str(raw.get("source_part") or "").strip()
        if source_part:
            normalized["source_part"] = source_part
        else:
            normalized.pop("source_part", None)
    return normalized


def normalize_redline_edits(
    raw_edits: object,
    *,
    allowed_actions: set[str] | frozenset[str] | None = None,
    require_content: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(raw_edits, list):
        return []
    return [
        normalized
        for normalized in (
            normalize_redline_edit(
                item,
                allowed_actions=allowed_actions,
                require_content=require_content,
            )
            for item in raw_edits
        )
        if normalized is not None
    ]


def clean_export_redline_contract(
    redline: object,
    allowed_actions: set[str] | frozenset[str],
) -> dict[str, str] | None:
    normalized = normalize_redline_edit(
        redline,
        allowed_actions=allowed_actions,
        require_content=True,
        trim_text=True,
    )
    if normalized is None:
        return None
    action = normalized["action"]
    return {
        "action": action,
        "paragraph_id": normalized["paragraph_id"],
        "original_text": normalized["original_text"],
        "replacement_text": normalized["replacement_text"],
        "anchor_text": str(normalized.get("anchor_text") or ""),
        "insert_text": redline_inserted_text(normalized).strip()
        if action == REDLINE_INSERT_AFTER_PARAGRAPH
        else "",
    }


def redline_source_index(redline: dict[str, Any]) -> int | None:
    if redline.get("source_part"):
        return None
    return _clean_index(redline.get("source_index", redline.get("paragraph_index")))


def redline_source_part(
    redline: dict[str, Any],
    review_paragraphs_by_id: dict[str, dict[str, Any]],
) -> str:
    source_part = str(redline.get("source_part") or "").strip()
    if source_part:
        return source_part
    review_paragraph = review_paragraphs_by_id.get(str(redline.get("paragraph_id") or ""))
    if isinstance(review_paragraph, dict):
        return str(review_paragraph.get("source_part") or "").strip()
    return ""


def redline_anchor_texts(
    redline: dict[str, Any],
    review_paragraphs_by_id: dict[str, dict[str, Any]],
    *,
    normalize_text: Callable[[object], str] = _normalize_paragraph_text,
) -> list[str]:
    candidates: list[object] = []
    if redline.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH:
        candidates.extend([redline.get("anchor_text"), redline.get("original_text")])
    else:
        candidates.extend([redline.get("original_text"), redline.get("anchor_text")])

    review_paragraph = review_paragraphs_by_id.get(str(redline.get("paragraph_id") or ""))
    if review_paragraph:
        candidates.append(review_paragraph.get("text"))

    texts: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = normalize_text(candidate)
        if not normalized or normalized in seen:
            continue
        texts.append(str(candidate or ""))
        seen.add(normalized)
    return texts


def redline_review_paragraph_key(redline: dict[str, Any]) -> tuple[str, str | int] | None:
    paragraph_id = str(redline.get("paragraph_id") or "").strip()
    if paragraph_id:
        return ("paragraph_id", paragraph_id)
    source_index = redline_source_index(redline)
    if source_index is not None:
        return ("source_index", source_index)
    return None


def redline_resolution_order(redline: dict[str, Any]) -> tuple[int, int]:
    paragraph_index = redline.get("paragraph_index")
    source_index = redline_source_index(redline)
    return (
        paragraph_index if isinstance(paragraph_index, int) else 1_000_000,
        source_index if isinstance(source_index, int) else 1_000_000,
    )


def _has_required_redline_text(
    action: str,
    *,
    original_text: str,
    replacement_text: str,
    insert_text: str,
) -> bool:
    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH, REDLINE_FORMAT_PARAGRAPH} and not original_text:
        return False
    if action == REDLINE_REPLACE_PARAGRAPH and not replacement_text:
        return False
    if action == REDLINE_INSERT_AFTER_PARAGRAPH and not (insert_text or replacement_text):
        return False
    return True


def _normalized_replacement_text(action: str, original_text: str, replacement_text: str) -> str:
    if action == REDLINE_DELETE_PARAGRAPH:
        return ""
    if action == REDLINE_FORMAT_PARAGRAPH:
        return original_text
    return replacement_text


def _clean_text(value: object, *, trim: bool) -> str:
    text = str(value or "")
    return text.strip() if trim else text


def _clean_index(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _copy_jsonish_dict(value: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            copied[key] = _copy_jsonish_dict(item)
        elif isinstance(item, list):
            copied[key] = [
                _copy_jsonish_dict(entry) if isinstance(entry, dict) else entry
                for entry in item
            ]
        else:
            copied[key] = item
    return copied
