from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import uuid4

from . import telemetry
from .durable_io import fsync_parent_directory
from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_FORMAT_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = Path(os.environ["NDA_EXPORTS_DIR"]).expanduser() if os.environ.get("NDA_EXPORTS_DIR") else None
MAX_SAVED_EXPORTS = 25
MAX_REVIEW_COMMENTS = 100
MAX_REVIEW_COMMENT_TEXT_CHARS = 2000


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
        if not _submitted_redline_identifies_server_redline(server_redline, submitted):
            continue
        selected.append(_server_redline_with_submitted_decision(server_redline, submitted))
    review_result["redline_edits"] = selected


def _submitted_redline_identifies_server_redline(server_redline: dict, submitted_redline: dict) -> bool:
    for key in ("clause_id", "paragraph_id", "action"):
        submitted_value = str(submitted_redline.get(key) or "").strip()
        if submitted_value and submitted_value != str(server_redline.get(key) or "").strip():
            return False
    return True


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


def apply_review_comments(review_result: dict, review_comments: object) -> None:
    cleaned_comments = clean_review_comments(review_comments)
    if cleaned_comments:
        review_result["review_comments"] = cleaned_comments
    else:
        review_result.pop("review_comments", None)


def clean_review_comments(review_comments: object) -> list[dict]:
    if not isinstance(review_comments, list):
        return []

    cleaned = []
    for comment in review_comments[:MAX_REVIEW_COMMENTS]:
        if not isinstance(comment, dict):
            continue
        text = " ".join(str(comment.get("text") or "").split())[:MAX_REVIEW_COMMENT_TEXT_CHARS]
        if not text:
            continue
        clause_id = str(comment.get("clause_id") or "").strip()[:120]
        paragraph_id = str(comment.get("paragraph_id") or "").strip()[:120]
        if not clause_id and not paragraph_id:
            continue
        clean_comment = {
            "id": str(comment.get("id") or f"comment-{clause_id or paragraph_id}").strip()[:160],
            "text": text,
        }
        for key in ("clause_id", "clause_name", "paragraph_id", "parent_id", "author", "created_at", "scope", "selected_text"):
            value = str(comment.get(key) or "").strip()
            if value:
                clean_comment[key] = value[:1000 if key == "selected_text" else 240]
        if bool(comment.get("resolved")):
            clean_comment["resolved"] = True
        _copy_comment_indexes(comment, clean_comment)
        _copy_comment_offsets(comment, clean_comment)
        cleaned.append(clean_comment)
    return cleaned


def clean_manual_export_redline(redline: object) -> dict | None:
    if not isinstance(redline, dict):
        return None

    common = _clean_export_redline_contract(
        redline,
        {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH, REDLINE_FORMAT_PARAGRAPH},
    )
    if common is None:
        return None

    action = common["action"]
    paragraph_id = common["paragraph_id"]

    cleaned = {
        "id": str(redline.get("id") or f"manual-{paragraph_id}"),
        "clause_id": "manual_viewer_edit",
        "status": "proposed",
        "action": action,
        "action_label": _MANUAL_REDLINE_ACTION_LABELS.get(action, "Replace paragraph"),
        "paragraph_id": paragraph_id,
        "original_text": common["original_text"],
        "replacement_text": common["replacement_text"],
    }

    if action == REDLINE_FORMAT_PARAGRAPH:
        cleaned["format_ops"] = _clean_format_ops(redline.get("format_ops"), common["original_text"])

    _copy_redline_indexes(redline, cleaned)
    source_part = str(redline.get("source_part") or "").strip()
    if source_part:
        cleaned["source_part"] = source_part
    return cleaned


_MANUAL_REDLINE_ACTION_LABELS = {
    REDLINE_REPLACE_PARAGRAPH: "Replace paragraph",
    REDLINE_DELETE_PARAGRAPH: "Remove paragraph",
    REDLINE_FORMAT_PARAGRAPH: "Format paragraph",
}

MAX_FORMAT_OPS = 200
MAX_FONT_NAME_CHARS = 120
_FORMAT_OP_SCOPES = {"paragraph", "run"}
_FORMAT_OP_PROPERTIES = {"alignment", "font", "bold", "italic"}
_FORMAT_OP_ALIGNMENTS = {"left", "center", "right", "justify"}


def _clean_format_ops(format_ops: object, original_text: str) -> list[dict]:
    """Sanitise the untrusted ``format_ops`` array from the export payload.

    The frontend sends these; treat every field as hostile. We clamp ``scope`` and
    ``property`` to known enums, alignment values to the four Word justifications,
    font names to a bounded string, and run ``start``/``end`` to valid offsets into
    the (equal) paragraph text. Unknown keys and unrecognised ops are dropped, and
    the op count is capped."""
    if not isinstance(format_ops, list):
        return []

    text_length = len(original_text)
    cleaned: list[dict] = []
    for op in format_ops[:MAX_FORMAT_OPS]:
        if not isinstance(op, dict):
            continue
        scope = str(op.get("scope") or "").strip().lower()
        if scope not in _FORMAT_OP_SCOPES:
            continue
        prop = str(op.get("property") or "").strip().lower()
        if prop not in _FORMAT_OP_PROPERTIES:
            continue

        clean_op: dict = {"scope": scope, "property": prop}
        if prop == "alignment":
            to_value = _clean_alignment_value(op.get("to"))
            if to_value is None:
                continue
            clean_op["to"] = to_value
            from_value = _clean_alignment_value(op.get("from"))
            if from_value is not None:
                clean_op["from"] = from_value
        elif prop == "font":
            clean_op["to"] = str(op.get("to") or "").strip()[:MAX_FONT_NAME_CHARS]
            clean_op["from"] = str(op.get("from") or "").strip()[:MAX_FONT_NAME_CHARS]
        else:
            # bold/italic: carry the truthy intent through for the later inline
            # milestone; the paragraph emitter ignores run-scope ops for now.
            clean_op["to"] = bool(op.get("to"))
            clean_op["from"] = bool(op.get("from"))

        if scope == "run":
            offsets = _clean_run_offsets(op.get("start"), op.get("end"), text_length)
            if offsets is None:
                continue
            clean_op["start"], clean_op["end"] = offsets

        cleaned.append(clean_op)
    return cleaned


def _clean_alignment_value(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _FORMAT_OP_ALIGNMENTS else None


def _clean_run_offsets(start: object, end: object, text_length: int) -> tuple[int, int] | None:
    try:
        start_index = int(start)
        end_index = int(end)
    except (TypeError, ValueError, OverflowError):
        return None
    if start_index < 0 or end_index < start_index or end_index > text_length:
        return None
    return start_index, end_index


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
    tmp_path: Path | None = None
    try:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        export_path = _collision_safe_export_path(safe_name)
        # Write to a temp file and atomically rename so a crash or full disk
        # never leaves a truncated, valid-looking .docx in the exports dir.
        descriptor, tmp_name = tempfile.mkstemp(dir=str(export_path.parent), prefix=".tmp-export-", suffix=".docx")
        tmp_path = Path(tmp_name)
        with os.fdopen(descriptor, "wb") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, export_path)
        tmp_path = None
        fsync_parent_directory(export_path)
        prune_saved_exports(export_path)
        return export_path
    except OSError as error:
        telemetry.increment("export_copy_failures")
        print(f"Could not save export copy atomically: {error.__class__.__name__}")
        return None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _collision_safe_export_path(safe_name: str) -> Path:
    if EXPORTS_DIR is None:
        raise OSError("Export directory is not configured.")
    export_path = (EXPORTS_DIR / safe_name).resolve()
    exports_dir = EXPORTS_DIR.resolve()
    if export_path.parent != exports_dir:
        export_path = (EXPORTS_DIR / "nda-review-report.docx").resolve()
    if not export_path.exists():
        return export_path

    suffix = export_path.suffix or ".docx"
    stem = export_path.stem or "nda-review-report"
    for _attempt in range(100):
        candidate = (EXPORTS_DIR / f"{stem}-{uuid4().hex[:12]}{suffix}").resolve()
        if candidate.parent == exports_dir and not candidate.exists():
            return candidate
    return (EXPORTS_DIR / f"{stem}-{uuid4().hex}{suffix}").resolve()


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
    # A format_paragraph redline leaves the TEXT unchanged (original == replacement),
    # so the empty-replacement reject must not fire for it -- require non-empty
    # original_text instead, and echo it back as the (equal) replacement_text.
    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH, REDLINE_FORMAT_PARAGRAPH} and not original_text.strip():
        return None
    if action == REDLINE_REPLACE_PARAGRAPH and not replacement_text.strip():
        return None
    if action == REDLINE_INSERT_AFTER_PARAGRAPH and not insert_text.strip():
        return None

    if action == REDLINE_DELETE_PARAGRAPH:
        cleaned_replacement_text = ""
    elif action == REDLINE_FORMAT_PARAGRAPH:
        cleaned_replacement_text = original_text
    else:
        cleaned_replacement_text = replacement_text

    return {
        "action": action,
        "paragraph_id": paragraph_id,
        "original_text": original_text,
        "replacement_text": cleaned_replacement_text,
        "anchor_text": anchor_text,
        "insert_text": insert_text,
    }


def _copy_redline_indexes(source: dict, target: dict, *, remove_invalid: bool = False) -> None:
    for key in ("paragraph_index", "source_index"):
        try:
            target[key] = int(source.get(key))
        except (TypeError, ValueError, OverflowError, KeyError):
            if remove_invalid:
                target.pop(key, None)


def _copy_comment_indexes(source: dict, target: dict) -> None:
    for key in ("paragraph_index", "source_index"):
        try:
            target[key] = int(source.get(key))
        except (TypeError, ValueError, OverflowError, KeyError):
            continue


def _copy_comment_offsets(source: dict, target: dict) -> None:
    for key in ("selection_start", "selection_end"):
        try:
            value = int(source.get(key))
        except (TypeError, ValueError, OverflowError, KeyError):
            continue
        if value >= 0:
            target[key] = value
