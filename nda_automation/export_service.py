from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import uuid4

from . import redline_edit_contract, telemetry
from .durable_io import fsync_parent_directory
from .redline_actions import (
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

    server_redlines = redline_edit_contract.normalize_redline_edits(review_result.get("redline_edits", []))
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
        # SECURITY: "author" is deliberately NOT carried through. A direct API caller
        # could otherwise inject an arbitrary w:author into the exported document; the
        # export write-site forces a fixed non-PII author (docx_comments.EXPORT_COMMENT_AUTHOR).
        for key in ("clause_id", "clause_name", "paragraph_id", "parent_id", "created_at", "scope", "selected_text"):
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
        redline_edit_contract.MANUAL_REDLINE_ACTIONS,
    )
    if common is None:
        return None

    action = common["action"]
    paragraph_id = common["paragraph_id"]

    cleaned = {
        "id": str(redline.get("id") or f"manual-{paragraph_id}"),
        "clause_id": redline_edit_contract.MANUAL_VIEWER_EDIT_CLAUSE_ID,
        "status": "proposed",
        "action": action,
        "action_label": redline_edit_contract.redline_action_label(action),
        "paragraph_id": paragraph_id,
        "original_text": common["original_text"],
        "replacement_text": common["replacement_text"],
    }

    if action == REDLINE_FORMAT_PARAGRAPH:
        cleaned["format_ops"] = _clean_format_ops(redline.get("format_ops"), common["original_text"])

    # When a replace redline carries the edited paragraph's run model, preserve it so
    # the export can re-emit the INSERTED text as formatted runs (bold/italic/font/
    # size) rather than plain <w:t>. Only honoured for replace paragraphs and only
    # when the runs' joined text exactly reconstructs the replacement_text; otherwise
    # the field is dropped and the export falls back to the plain char/word diff.
    if action == REDLINE_REPLACE_PARAGRAPH:
        replacement_runs = _clean_replacement_runs(
            redline.get("replacement_runs"), common["replacement_text"]
        )
        if replacement_runs is not None:
            cleaned["replacement_runs"] = replacement_runs

    # Preserve whether this manual edit is a whole-paragraph replacement (e.g. a
    # clause/governing-law pick) vs. a free-form character-level edit, so the export
    # can route between the token/whole path and the char-level path. The sanitiser
    # otherwise drops this flag.
    cleaned["whole_paragraph"] = bool(redline.get("whole_paragraph"))

    _copy_redline_indexes(redline, cleaned)
    source_part = str(redline.get("source_part") or "").strip()
    if source_part:
        cleaned["source_part"] = source_part
    return cleaned


MAX_FORMAT_OPS = 200
MAX_FONT_NAME_CHARS = 120
MAX_REPLACEMENT_RUNS = 2000
_FORMAT_OP_SCOPES = {"paragraph", "run"}
# Properties are matched case-insensitively (the payload is lowercased before the
# membership test), so ``vertalign`` here matches the frontend's ``vertAlign`` op;
# it is canonicalised back to camelCase on emit so the redline_xml applier (which
# checks ``"vertAlign"``) recognises it.
_FORMAT_OP_PROPERTIES = {
    "alignment",
    "font",
    "bold",
    "italic",
    "size",
    "underline",
    "strike",
    "color",
    "highlight",
    "vertalign",
}
_FORMAT_OP_PARAGRAPH_PROPERTIES = {"alignment", "font", "size"}
_FORMAT_OP_RUN_PROPERTIES = {
    "bold",
    "italic",
    "font",
    "size",
    "underline",
    "strike",
    "color",
    "highlight",
    "vertalign",
}
# Lowercased-property -> canonical camelCase emitted on the cleaned op. Only props
# whose canonical token isn't already all-lowercase need an entry.
_FORMAT_OP_PROPERTY_CANONICAL = {"vertalign": "vertAlign"}
_FORMAT_OP_VERT_ALIGNS = {"superscript", "subscript"}
_FORMAT_OP_ALIGNMENTS = {"left", "center", "right", "justify"}
_FORMAT_OP_HIGHLIGHTS = {
    "black": "black",
    "blue": "blue",
    "cyan": "cyan",
    "darkblue": "darkBlue",
    "darkcyan": "darkCyan",
    "darkgray": "darkGray",
    "darkgreen": "darkGreen",
    "darkmagenta": "darkMagenta",
    "darkred": "darkRed",
    "darkyellow": "darkYellow",
    "green": "green",
    "lightgray": "lightGray",
    "magenta": "magenta",
    "none": "none",
    "red": "red",
    "white": "white",
    "yellow": "yellow",
}


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
        if scope == "paragraph" and prop not in _FORMAT_OP_PARAGRAPH_PROPERTIES:
            continue
        if scope == "run" and prop not in _FORMAT_OP_RUN_PROPERTIES:
            continue

        # Emit the canonical property token (e.g. vertalign -> vertAlign) so the
        # downstream redline_xml applier, which matches camelCase, recognises it.
        canonical_prop = _FORMAT_OP_PROPERTY_CANONICAL.get(prop, prop)
        clean_op: dict = {"scope": scope, "property": canonical_prop}
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
        elif prop == "size":
            clean_op["to"] = _clean_size_value(op.get("to"))
            clean_op["from"] = _clean_size_value(op.get("from"))
        elif prop == "color":
            clean_op["to"] = _clean_hex_color(op.get("to"))
            clean_op["from"] = _clean_hex_color(op.get("from"))
            if not clean_op["to"]:
                continue
        elif prop == "highlight":
            clean_op["to"] = _clean_highlight_value(op.get("to"))
            clean_op["from"] = _clean_highlight_value(op.get("from"))
            if not clean_op["to"]:
                continue
        elif prop == "vertalign":
            to_value = str(op.get("to") or "").strip().lower()
            if to_value not in _FORMAT_OP_VERT_ALIGNS:
                continue
            clean_op["to"] = to_value
            from_value = str(op.get("from") or "").strip().lower()
            clean_op["from"] = from_value if from_value in _FORMAT_OP_VERT_ALIGNS else ""
        else:
            # Boolean run toggles.
            clean_op["to"] = bool(op.get("to"))
            clean_op["from"] = bool(op.get("from"))

        if scope == "run":
            offsets = _clean_run_offsets(op.get("start"), op.get("end"), text_length)
            if offsets is None:
                continue
            clean_op["start"], clean_op["end"] = offsets

        cleaned.append(clean_op)
    return cleaned


def _clean_replacement_runs(replacement_runs: object, replacement_text: str) -> list[dict] | None:
    """Sanitise the untrusted ``replacement_runs`` array carried by a replace redline.

    The frontend attaches the edited paragraph's run model so the clean export can
    re-emit the inserted text WITH its formatting (bold/italic/font/size) instead of
    plain text. Every field is treated as hostile: ``text`` is coerced to str, the
    boolean toggles to bool, ``font`` clamped to a bounded name, ``size`` clamped to
    Word's valid point range, and unknown keys dropped. The run count is capped.

    Returns the cleaned list ONLY when the runs' joined text exactly equals
    ``replacement_text`` -- otherwise the run model disagrees with the text the export
    is about to insert, so we return ``None`` and let the caller fall back to the plain
    char/word diff. An empty/whitespace-only list is also rejected."""
    if not isinstance(replacement_runs, list) or not replacement_runs:
        return None

    cleaned: list[dict] = []
    for run in replacement_runs[:MAX_REPLACEMENT_RUNS]:
        if not isinstance(run, dict):
            return None
        clean_run: dict = {"text": str(run.get("text") or "")}
        if bool(run.get("bold")):
            clean_run["bold"] = True
        if bool(run.get("italic")):
            clean_run["italic"] = True
        if bool(run.get("underline")):
            clean_run["underline"] = True
        if bool(run.get("strike")):
            clean_run["strike"] = True
        font = str(run.get("font") or "").strip()[:MAX_FONT_NAME_CHARS]
        if font:
            clean_run["font"] = font
        size = _clean_size_value(run.get("size"))
        if size:
            clean_run["size"] = size
        color = _clean_hex_color(run.get("color"))
        if color:
            clean_run["color"] = color
        highlight = _clean_highlight_value(run.get("highlight"))
        if highlight:
            clean_run["highlight"] = highlight
        vert_align = str(run.get("vertAlign") or "").strip().lower()
        if vert_align in _FORMAT_OP_VERT_ALIGNS:
            clean_run["vertAlign"] = vert_align
        cleaned.append(clean_run)

    if "".join(run["text"] for run in cleaned) != replacement_text:
        return None
    return cleaned


def _clean_alignment_value(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _FORMAT_OP_ALIGNMENTS else None


def _clean_size_value(value: object) -> int:
    """Coerce a requested point size to an int in Word's valid range; 0 = none."""
    try:
        size = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0
    if size <= 0:
        return 0
    return min(max(size, 1), 1638)


def _clean_hex_color(value: object) -> str:
    normalized = str(value or "").strip().lstrip("#").upper()
    if len(normalized) != 6:
        return ""
    return normalized if all(character in "0123456789ABCDEF" for character in normalized) else ""


def _clean_highlight_value(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return _FORMAT_OP_HIGHLIGHTS.get(normalized, "")


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
        # In-flight atomic writes land as ``.tmp-export-*.docx`` temp files (pathlib's
        # glob matches leading-dot names, unlike the shell). Excluding them keeps a
        # concurrent export's half-written temp from counting toward the cap or being
        # unlinked mid-write.
        if path.is_file() and not path.name.startswith(".tmp-export-")
    ]
    if len(saved_exports) <= MAX_SAVED_EXPORTS:
        return

    protected_path = protected_path.resolve()
    saved_exports.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    removable_exports = [path for path in saved_exports[MAX_SAVED_EXPORTS:] if path.resolve() != protected_path]
    for path in removable_exports:
        path.unlink(missing_ok=True)


def _clean_export_redline_contract(redline: object, allowed_actions: set[str] | frozenset[str]) -> dict | None:
    return redline_edit_contract.clean_export_redline_contract(redline, allowed_actions)


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
