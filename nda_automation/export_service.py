from __future__ import annotations

import os
from pathlib import Path

from . import telemetry
from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = Path(os.environ["NDA_EXPORTS_DIR"]).expanduser() if os.environ.get("NDA_EXPORTS_DIR") else None
MAX_SAVED_EXPORTS = 25


def redline_download_filename(filename: str) -> str:
    source_name = Path(filename).stem if filename else ""
    safe_name = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in source_name)
    safe_name = safe_name.strip("-_") or "nda"
    return f"{safe_name}-redlined.docx"


def apply_selected_export_redlines(review_result: dict, selected_redlines: object) -> None:
    if not isinstance(selected_redlines, list):
        return

    server_redlines = review_result.get("redline_edits", [])
    if not isinstance(server_redlines, list):
        server_redlines = []
    server_redlines_by_id = {
        str(redline.get("id")): redline
        for redline in server_redlines
        if isinstance(redline, dict) and redline.get("id")
    }

    selected = []
    for submitted in selected_redlines:
        if not isinstance(submitted, dict):
            continue
        server_redline = server_redlines_by_id.get(str(submitted.get("id") or ""))
        if server_redline is None:
            continue
        selected.append(_server_redline_with_submitted_decision(server_redline, submitted))
    review_result["redline_edits"] = selected


def apply_manual_export_redlines(review_result: dict, manual_redlines: object) -> None:
    if not isinstance(manual_redlines, list):
        return

    cleaned_redlines = [
        redline
        for redline in (clean_manual_export_redline(item) for item in manual_redlines)
        if redline is not None
    ]
    if not cleaned_redlines:
        return

    manual_paragraph_ids = {str(redline.get("paragraph_id")) for redline in cleaned_redlines}
    existing_redlines = review_result.get("redline_edits", [])
    if not isinstance(existing_redlines, list):
        existing_redlines = []
    review_result["redline_edits"] = cleaned_redlines + [
        redline
        for redline in existing_redlines
        if not (isinstance(redline, dict) and str(redline.get("paragraph_id")) in manual_paragraph_ids)
    ]


def clean_manual_export_redline(redline: object) -> dict | None:
    if not isinstance(redline, dict):
        return None

    common = _clean_export_redline_contract(redline, {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH})
    if common is None:
        return None

    action = common["action"]
    paragraph_id = common["paragraph_id"]

    cleaned = {
        "id": str(redline.get("id") or f"manual-{paragraph_id}"),
        "clause_id": "manual_viewer_edit",
        "status": "proposed",
        "action": action,
        "action_label": "Remove paragraph" if action == REDLINE_DELETE_PARAGRAPH else "Replace paragraph",
        "paragraph_id": paragraph_id,
        "original_text": common["original_text"],
        "replacement_text": common["replacement_text"],
    }

    _copy_redline_indexes(redline, cleaned)
    return cleaned


def _server_redline_with_submitted_decision(server_redline: dict, submitted_redline: dict) -> dict:
    redline = _copy_jsonish_dict(server_redline)
    selected_option_id = _submitted_selected_template_option_id(submitted_redline)
    if selected_option_id:
        _apply_server_template_selection(redline, selected_option_id)
    return redline


def _submitted_selected_template_option_id(submitted_redline: dict) -> str:
    template_options = submitted_redline.get("template_options")
    if not isinstance(template_options, list):
        return ""
    for option in template_options:
        if isinstance(option, dict) and option.get("selected") and option.get("id"):
            return str(option.get("id"))
    return ""


def _apply_server_template_selection(redline: dict, selected_option_id: str) -> None:
    template_options = redline.get("template_options")
    if not isinstance(template_options, list):
        return

    selected_option = None
    updated_options = []
    for option in template_options:
        if not isinstance(option, dict):
            continue
        copied_option = _copy_jsonish_dict(option)
        copied_option["selected"] = str(copied_option.get("id") or "") == selected_option_id
        if copied_option["selected"]:
            selected_option = copied_option
        updated_options.append(copied_option)

    if selected_option is None:
        return

    redline["template_options"] = updated_options
    replacement_text = str(
        selected_option.get("replacement_text")
        or selected_option.get("text")
        or ""
    ).strip()
    insert_text = str(
        selected_option.get("insert_text")
        or selected_option.get("replacement_text")
        or selected_option.get("text")
        or ""
    ).strip()
    if redline.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH:
        if insert_text:
            redline["insert_text"] = insert_text
            redline["replacement_text"] = insert_text
    elif replacement_text:
        redline["replacement_text"] = replacement_text
        if isinstance(selected_option.get("inline_diff_operations"), list):
            redline["inline_diff_operations"] = selected_option["inline_diff_operations"]


def _copy_jsonish_dict(value: dict) -> dict:
    copied = {}
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


def persist_export(data: bytes, filename: str) -> Path | None:
    if EXPORTS_DIR is None:
        return None
    safe_name = os.path.basename(filename) or "nda-review-report.docx"
    try:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        export_path = (EXPORTS_DIR / safe_name).resolve()
        if export_path.parent != EXPORTS_DIR.resolve():
            export_path = EXPORTS_DIR / "nda-review-report.docx"
        export_path.write_bytes(data)
        prune_saved_exports(export_path)
        return export_path
    except OSError as error:
        telemetry.increment("export_copy_failures")
        print(f"Could not save export copy: {error.__class__.__name__}")
        return None


def prune_saved_exports(protected_path: Path) -> None:
    if EXPORTS_DIR is None:
        return
    saved_exports = [
        path
        for path in EXPORTS_DIR.glob("*.docx")
        if path.is_file()
    ]
    if len(saved_exports) <= MAX_SAVED_EXPORTS:
        return

    protected_path = protected_path.resolve()
    saved_exports.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    removable_exports = [path for path in saved_exports[MAX_SAVED_EXPORTS:] if path.resolve() != protected_path]
    for path in removable_exports:
        path.unlink(missing_ok=True)


def _clean_export_redline_contract(redline: dict, allowed_actions: set[str]) -> dict | None:
    action = redline.get("action")
    if action not in allowed_actions:
        return None

    paragraph_id = str(redline.get("paragraph_id") or "").strip()
    if not paragraph_id:
        return None

    original_text = str(redline.get("original_text") or "").strip()
    replacement_text = str(redline.get("replacement_text") or "").strip()
    anchor_text = str(redline.get("anchor_text") or "").strip()
    insert_text = str(redline.get("insert_text") or "").strip()
    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH} and not original_text.strip():
        return None
    if action == REDLINE_REPLACE_PARAGRAPH and not replacement_text.strip():
        return None
    if action == REDLINE_INSERT_AFTER_PARAGRAPH and not insert_text.strip():
        return None

    return {
        "action": action,
        "paragraph_id": paragraph_id,
        "original_text": original_text,
        "replacement_text": "" if action == REDLINE_DELETE_PARAGRAPH else replacement_text,
        "anchor_text": anchor_text,
        "insert_text": insert_text,
    }


def _copy_redline_indexes(source: dict, target: dict, *, remove_invalid: bool = False) -> None:
    for key in ("paragraph_index", "source_index"):
        try:
            target[key] = int(source.get(key))
        except (TypeError, ValueError, KeyError):
            if remove_invalid:
                target.pop(key, None)
