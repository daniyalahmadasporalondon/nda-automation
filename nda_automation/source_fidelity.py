from __future__ import annotations

from collections import defaultdict
from typing import Any

SOURCE_FIDELITY_CONTRACT_VERSION = 1

STYLE_KEYS = (
    "alignment",
    "font",
    "fontSize",
    "heading_level",
    "indent_left",
    "numbering",
    "outline_level",
    "source_part",
    "structure_label",
    "structure_number",
    "style_id",
    "style_name",
)

RUN_STYLE_KEYS = (
    "bold",
    "color",
    "font",
    "highlight",
    "italic",
    "size",
    "strike",
    "underline",
    "vertAlign",
)


def source_fidelity_payload(review_result: dict[str, Any], *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the additive source-fidelity contract for Review rendering.

    The legal review model remains paragraph based. This payload exposes the
    layout/style facts the extractor can prove, so UI renderers can preserve
    tables and inline colors without making clause analysis depend on layout.
    """
    paragraphs = [paragraph for paragraph in review_result.get("paragraphs", []) if isinstance(paragraph, dict)]
    blocks = _source_blocks(paragraphs)
    summary = _summary(paragraphs, blocks)
    source_type = _source_type(source, paragraphs)
    extraction_quality = _extraction_quality(source)
    pdf_visual_profile = _pdf_visual_profile(extraction_quality)
    payload: dict[str, Any] = {
        "version": SOURCE_FIDELITY_CONTRACT_VERSION,
        "source_type": source_type,
        "analysis_model": "paragraphs",
        "render_model": "source_blocks",
        "preferred_render_mode": "source_pdf_preview" if source_type == "pdf" else "source_blocks",
        "blocks": blocks,
        "capabilities": {
            "structured_tables": summary["table_count"] > 0,
            "table_cell_styles": summary["styled_table_cell_count"] > 0,
            "table_cell_backgrounds": summary["table_cell_background_count"] > 0,
            "inline_runs": summary["styled_run_count"] > 0,
            "run_colors": summary["color_run_count"] > 0,
            "pdf_page_references": summary["pdf_page_reference_count"] > 0,
            "faithful_source_preview": source_type == "pdf",
            "pdf_visual_profile": bool(pdf_visual_profile),
            "pdf_visual_elements": _pdf_requires_source_preview(pdf_visual_profile),
        },
        "summary": summary,
        "limitations": _limitations(source_type, pdf_visual_profile=pdf_visual_profile),
    }
    if source_type == "pdf":
        payload["pdf_fidelity"] = _pdf_fidelity_policy(pdf_visual_profile)
    return payload


def _source_blocks(paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    table_groups: dict[int, dict[int, dict[int, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    table_order: list[int] = []
    table_positions: dict[int, int] = {}
    table_cell_styles: dict[tuple[int, int, int], dict[str, Any]] = {}

    for paragraph in paragraphs:
        table = paragraph.get("table")
        if isinstance(table, dict):
            table_index = _positive_int(table.get("table_index"))
            row_index = _positive_int(table.get("row_index"))
            cell_index = _positive_int(table.get("cell_index"))
            if table_index and row_index and cell_index:
                if table_index not in table_groups:
                    table_order.append(table_index)
                    table_positions[table_index] = len(blocks)
                    blocks.append({"type": "table", "id": f"table-{table_index}", "table_index": table_index})
                cell_style = _table_cell_style_record(table)
                if cell_style:
                    table_cell_styles.setdefault((table_index, row_index, cell_index), cell_style)
                table_groups[table_index][row_index][cell_index].append(_paragraph_block(paragraph, in_table=True))
                continue
        blocks.append(_paragraph_block(paragraph, in_table=False))

    for table_index in table_order:
        rows = []
        for row_index in sorted(table_groups[table_index]):
            cells = []
            for cell_index in sorted(table_groups[table_index][row_index]):
                cell_blocks = table_groups[table_index][row_index][cell_index]
                cell = {
                    "cell_index": cell_index,
                    "paragraph_ids": [str(block["paragraph_id"]) for block in cell_blocks],
                    "blocks": cell_blocks,
                }
                cell_style = table_cell_styles.get((table_index, row_index, cell_index))
                if cell_style:
                    cell["style"] = cell_style
                cells.append(cell)
            rows.append({"row_index": row_index, "cells": cells})
        blocks[table_positions[table_index]] = {
            "type": "table",
            "id": f"table-{table_index}",
            "table_index": table_index,
            "rows": rows,
        }
    return blocks


def _paragraph_block(paragraph: dict[str, Any], *, in_table: bool) -> dict[str, Any]:
    paragraph_id = str(paragraph.get("id") or "")
    block: dict[str, Any] = {
        "type": "paragraph",
        "id": paragraph_id or f"source-{paragraph.get('source_index') or ''}",
        "paragraph_id": paragraph_id,
        "text": str(paragraph.get("text") or ""),
        "source_kind": str(paragraph.get("source_kind") or ("table_cell" if in_table else "paragraph")),
    }
    for key in ("index", "source_index", "start", "end", "page_number"):
        if key in paragraph:
            block[key] = paragraph[key]
    style = _style_record(paragraph)
    if style:
        block["style"] = style
    runs = _run_records(paragraph.get("runs"))
    if runs:
        block["runs"] = runs
    return block


def _style_record(paragraph: dict[str, Any]) -> dict[str, Any]:
    return {
        key: paragraph[key]
        for key in STYLE_KEYS
        if key in paragraph and paragraph[key] not in (None, "", {})
    }


def _run_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    runs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        if not text:
            continue
        run = {"text": text}
        for key in RUN_STYLE_KEYS:
            if key in item and item[key] not in (None, "", False):
                run[key] = item[key]
            elif key in ("bold", "italic", "underline") and key in item:
                run[key] = bool(item[key])
        runs.append(run)
    return runs


def _table_cell_style_record(table: dict[str, Any]) -> dict[str, Any]:
    value = table.get("cell_style")
    if not isinstance(value, dict):
        return {}
    style: dict[str, Any] = {}
    background_color = _hex_color(value.get("background_color"))
    if background_color:
        style["background_color"] = background_color
    width = value.get("width")
    if isinstance(width, dict):
        width_record: dict[str, Any] = {}
        try:
            width_value = int(width.get("value"))
        except (TypeError, ValueError):
            width_value = 0
        if width_value > 0:
            width_record["value"] = width_value
        width_type = str(width.get("type") or "").strip().lower()
        if width_type:
            width_record["type"] = width_type
        if width_record:
            style["width"] = width_record
    return style


def _summary(paragraphs: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> dict[str, int]:
    styled_run_count = 0
    color_run_count = 0
    pdf_page_reference_count = 0
    styled_table_cell_count = 0
    table_cell_background_count = 0
    for paragraph in paragraphs:
        if paragraph.get("page_number") is not None:
            pdf_page_reference_count += 1
        for run in _run_records(paragraph.get("runs")):
            if _run_has_visible_style(run):
                styled_run_count += 1
            if run.get("color"):
                color_run_count += 1
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "table":
            continue
        for row in block.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for cell in row.get("cells") or []:
                if not isinstance(cell, dict):
                    continue
                style = cell.get("style")
                if not isinstance(style, dict) or not style:
                    continue
                styled_table_cell_count += 1
                if style.get("background_color"):
                    table_cell_background_count += 1
    return {
        "paragraph_count": len(paragraphs),
        "block_count": len(blocks),
        "table_count": sum(1 for block in blocks if block.get("type") == "table"),
        "styled_table_cell_count": styled_table_cell_count,
        "table_cell_background_count": table_cell_background_count,
        "styled_run_count": styled_run_count,
        "color_run_count": color_run_count,
        "pdf_page_reference_count": pdf_page_reference_count,
    }


def _source_type(source: dict[str, Any] | None, paragraphs: list[dict[str, Any]]) -> str:
    if isinstance(source, dict):
        source_type = str(source.get("type") or "").strip().lower()
        if source_type:
            return source_type
    if any(str(paragraph.get("source_part") or "") == "pdf" for paragraph in paragraphs):
        return "pdf"
    if any(isinstance(paragraph.get("table"), dict) or paragraph.get("runs") for paragraph in paragraphs):
        return "docx"
    return "unknown"


def _run_has_visible_style(run: dict[str, Any]) -> bool:
    for key in RUN_STYLE_KEYS:
        value = run.get(key)
        if value not in (None, "", False):
            return True
    return False


def _hex_color(value: Any) -> str:
    color = str(value or "").strip().lower()
    if color.startswith("#"):
        color = color[1:]
    if len(color) == 6 and all(character in "0123456789abcdef" for character in color):
        return f"#{color}"
    return ""


def _limitations(source_type: str, *, pdf_visual_profile: dict[str, Any] | None = None) -> list[dict[str, str]]:
    limitations = [
        {
            "code": "semantic_review_is_paragraph_based",
            "message": "Clause analysis and redlines still target review paragraphs; visual layout is exposed separately.",
        }
    ]
    if source_type == "pdf":
        limitations.append({
            "code": "pdf_visual_fidelity_requires_source_preview",
            "message": "PDF extraction provides text and page references; faithful visual review should use the source PDF/page preview.",
        })
        limitations.append({
            "code": "pdf_text_extraction_not_layout",
            "message": "PDF text extraction does not preserve tables, colors, borders, images, or exact page layout.",
        })
        limitations.append({
            "code": "pdf_word_conversion_unsupported_for_fidelity",
            "message": "The backend preserves the original PDF for visual review instead of presenting extracted text as a faithful Word conversion.",
        })
        if _pdf_visual_profile_unavailable(pdf_visual_profile):
            limitations.append({
                "code": "pdf_visual_profile_unavailable",
                "message": (
                    "PDF visual profiling is unavailable in this runtime, so the backend cannot verify tables, colors, "
                    "borders, images, or exact layout from extracted text. Use the source PDF/page preview for visual review."
                ),
            })
        if _pdf_requires_source_preview(pdf_visual_profile):
            limitations.append({
                "code": "pdf_visual_elements_detected",
                "message": "The PDF contains visual layout signals that require Original PDF/page preview for a faithful review surface.",
            })
    return limitations


def _extraction_quality(source: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    value = source.get("extraction_quality")
    return value if isinstance(value, dict) else {}


def _pdf_visual_profile(extraction_quality: dict[str, Any]) -> dict[str, Any] | None:
    value = extraction_quality.get("visual_profile")
    return value if isinstance(value, dict) else None


def _pdf_requires_source_preview(pdf_visual_profile: dict[str, Any] | None) -> bool:
    if not isinstance(pdf_visual_profile, dict):
        return False
    return bool(pdf_visual_profile.get("requires_source_preview"))


def _pdf_visual_profile_unavailable(pdf_visual_profile: dict[str, Any] | None) -> bool:
    if not isinstance(pdf_visual_profile, dict):
        return False
    return str(pdf_visual_profile.get("status") or "").strip().lower() == "unavailable"


def _pdf_fidelity_policy(pdf_visual_profile: dict[str, Any] | None) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "analysis_mode": "extracted_text_only",
        "layout_mode": "original_pdf_page_preview",
        "word_conversion": "unsupported_for_fidelity",
        "redlined_docx": "reconstructed_not_fidelity_preserving",
        "requires_source_preview": True,
        "message": (
            "PDF matters use extracted text for clause analysis, best-effort reconstructed Word for editable exports, "
            "and the preserved original PDF/page preview for visual fidelity. Reconstructed Word must not be presented "
            "as a faithful source conversion."
        ),
    }
    if pdf_visual_profile:
        policy["visual_profile"] = pdf_visual_profile
    return policy


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
