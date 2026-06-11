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
    return {
        "version": SOURCE_FIDELITY_CONTRACT_VERSION,
        "source_type": source_type,
        "analysis_model": "paragraphs",
        "render_model": "source_blocks",
        "blocks": blocks,
        "capabilities": {
            "structured_tables": summary["table_count"] > 0,
            "inline_runs": summary["styled_run_count"] > 0,
            "run_colors": summary["color_run_count"] > 0,
            "pdf_page_references": summary["pdf_page_reference_count"] > 0,
        },
        "summary": summary,
        "limitations": _limitations(source_type),
    }


def _source_blocks(paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    table_groups: dict[int, dict[int, dict[int, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    table_order: list[int] = []
    table_positions: dict[int, int] = {}

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
                table_groups[table_index][row_index][cell_index].append(_paragraph_block(paragraph, in_table=True))
                continue
        blocks.append(_paragraph_block(paragraph, in_table=False))

    for table_index in table_order:
        rows = []
        for row_index in sorted(table_groups[table_index]):
            cells = []
            for cell_index in sorted(table_groups[table_index][row_index]):
                cell_blocks = table_groups[table_index][row_index][cell_index]
                cells.append({
                    "cell_index": cell_index,
                    "paragraph_ids": [str(block["paragraph_id"]) for block in cell_blocks],
                    "blocks": cell_blocks,
                })
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


def _summary(paragraphs: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> dict[str, int]:
    styled_run_count = 0
    color_run_count = 0
    pdf_page_reference_count = 0
    for paragraph in paragraphs:
        if paragraph.get("page_number") is not None:
            pdf_page_reference_count += 1
        for run in _run_records(paragraph.get("runs")):
            if _run_has_visible_style(run):
                styled_run_count += 1
            if run.get("color"):
                color_run_count += 1
    return {
        "paragraph_count": len(paragraphs),
        "block_count": len(blocks),
        "table_count": sum(1 for block in blocks if block.get("type") == "table"),
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


def _limitations(source_type: str) -> list[dict[str, str]]:
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
    return limitations


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
