from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, List, NamedTuple, Tuple
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .inline_diff import diff_text_operations
from .docx_health import validate_docx_open_health as validate_docx_open_health

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
SETTINGS_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
OFFICE_DOCUMENT_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
SETTINGS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
A4_PAGE_WIDTH_TWIPS = "11906"
A4_PAGE_HEIGHT_TWIPS = "16838"
ET.register_namespace("w", W_NS)
ET.register_namespace("r", R_NS)

ClauseResult = Dict[str, object]
Paragraph = Dict[str, object]
RedlineEdit = Dict[str, object]
ReviewResult = Dict[str, object]
LOGGER = logging.getLogger(__name__)


class SourceParagraph(NamedTuple):
    source_index: int
    parent: ET.Element
    paragraph: ET.Element
    text: str
    normalized_text: str


class DocxExportError(ValueError):
    """Raised when a DOCX cannot be patched into a redlined export."""


def build_review_report_docx(review_result: ReviewResult, title: str = "NDA Review") -> bytes:
    checked_at = str(review_result.get("checked_at", ""))
    paragraphs = [
        _paragraph("NDA Redline", style="Title"),
        _paragraph(f"Matter: {title or 'Untitled NDA'}", style="Subtitle"),
        _paragraph(
            "The Redlined NDA section contains native Word tracked changes. Review Notes are explanatory only. "
            "Track Changes is also enabled for any edits made after opening the file.",
            style="Note",
        ),
        _paragraph("Redlined NDA", style="Heading1"),
        *_redlined_nda_section(review_result),
        _paragraph("Review Notes", style="Heading1"),
        _paragraph(f"Overall status: {_status_label(str(review_result.get('overall_status', '')))}"),
        _paragraph(f"Requirements passed: {review_result.get('requirements_passed', 0)}"),
        _paragraph(f"Requirements failed: {review_result.get('requirements_failed', 0)}"),
        _paragraph(f"Checked at: {checked_at}"),
        _paragraph("Clause Findings", style="Heading1"),
    ]

    redlines_by_clause = _redlines_by_clause(review_result.get("redline_edits", []))
    for clause in review_result.get("clauses", []):
        if not isinstance(clause, dict):
            continue
        paragraphs.extend(_clause_section(clause, redlines_by_clause.get(str(clause.get("id")), [])))

    document_xml = _document_xml("".join(paragraphs))

    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _content_types_xml())
            archive.writestr("_rels/.rels", _package_rels_xml())
            archive.writestr("docProps/core.xml", _core_properties_xml(title or "NDA Review"))
            archive.writestr("docProps/app.xml", _app_properties_xml())
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/styles.xml", _styles_xml())
            archive.writestr("word/settings.xml", _settings_xml())
            archive.writestr("word/_rels/document.xml.rels", _document_rels_xml())
        return output.getvalue()


def build_source_redline_docx(source_docx: bytes, review_result: ReviewResult) -> bytes:
    try:
        with ZipFile(BytesIO(source_docx), "r") as source_archive:
            source_names = set(source_archive.namelist())
            document_root = ET.fromstring(source_archive.read("word/document.xml"))
            _apply_redline_edits_to_source_document(
                document_root,
                review_result.get("redline_edits", []),
                review_result.get("paragraphs", []),
            )
            _ensure_document_section_properties(document_root)

            overrides: Dict[str, bytes] = {
                "word/document.xml": _xml_bytes(document_root),
                "word/settings.xml": _settings_xml_with_track_revisions(
                    source_archive.read("word/settings.xml") if "word/settings.xml" in source_names else None
                ),
                "word/_rels/document.xml.rels": _document_rels_xml_with_settings(
                    source_archive.read("word/_rels/document.xml.rels")
                    if "word/_rels/document.xml.rels" in source_names
                    else None
                ),
                "[Content_Types].xml": _content_types_xml_with_settings(
                    source_archive.read("[Content_Types].xml") if "[Content_Types].xml" in source_names else None
                ),
                "_rels/.rels": _package_rels_xml_with_document(
                    source_archive.read("_rels/.rels") if "_rels/.rels" in source_names else None
                ),
            }

            with BytesIO() as output:
                with ZipFile(output, "w", ZIP_DEFLATED) as redlined_archive:
                    written = set()
                    for item in source_archive.infolist():
                        if item.filename in overrides:
                            data = overrides.pop(item.filename)
                        else:
                            data = source_archive.read(item.filename)
                        redlined_archive.writestr(item, data)
                        written.add(item.filename)
                    for name, data in overrides.items():
                        if name not in written:
                            redlined_archive.writestr(name, data)
                return output.getvalue()
    except (BadZipFile, KeyError, ET.ParseError) as exc:
        raise DocxExportError("The uploaded Word document could not be redlined.") from exc


def _redlined_nda_section(review_result: ReviewResult) -> List[str]:
    paragraphs = review_result.get("paragraphs", [])
    if not isinstance(paragraphs, list) or not paragraphs:
        return [_paragraph("No source paragraphs available.")]

    redlines_by_paragraph = _redlines_by_paragraph(review_result.get("redline_edits", []))
    output: List[str] = []
    revision_id = 1

    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue

        paragraph_id = str(paragraph.get("id", ""))
        paragraph_text = str(paragraph.get("text", ""))
        edits = redlines_by_paragraph.get(paragraph_id, [])
        primary_edit = next((edit for edit in edits if edit.get("action") != REDLINE_INSERT_AFTER_PARAGRAPH), None)

        if primary_edit and primary_edit.get("action") == REDLINE_REPLACE_PARAGRAPH:
            paragraph_xml, revision_id = _tracked_replace_paragraph(
                str(primary_edit.get("original_text") or paragraph_text),
                str(primary_edit.get("replacement_text") or ""),
                revision_id,
            )
            output.append(paragraph_xml)
        elif primary_edit and primary_edit.get("action") == REDLINE_DELETE_PARAGRAPH:
            output.append(_tracked_delete_paragraph(str(primary_edit.get("original_text") or paragraph_text), revision_id))
            revision_id += 1
        else:
            output.append(_paragraph(paragraph_text))

        for insertion in [edit for edit in edits if edit.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH]:
            for insert_paragraph in _tracked_insert_paragraphs(str(insertion.get("insert_text") or insertion.get("replacement_text") or ""), revision_id):
                output.append(insert_paragraph)
                revision_id += 1

    return output


def _clause_section(clause: ClauseResult, redlines: List[RedlineEdit]) -> List[str]:
    status = "PASS" if clause.get("passes") else "CHECK"
    output = [
        _paragraph(f"{clause.get('name', 'Clause')} - {status}", style="Heading2"),
        _label_value("Requirement", clause.get("requirement")),
        _label_value("Issue type", clause.get("issue_label")),
        _label_value("Why", clause.get("reason") or clause.get("finding")),
        _label_value("What to fix", clause.get("what_to_fix")),
        _label_value("Exact paragraph", clause.get("matched_text") or "No matching paragraph identified."),
    ]

    if redlines:
        output.append(_paragraph("Proposed Redline", style="Heading3"))
        for redline in redlines:
            output.extend(_redline_section(redline))
    return output


def _redline_section(redline: RedlineEdit) -> List[str]:
    action = str(redline.get("action_label") or "Proposed edit")
    paragraph_id = str(redline.get("paragraph_id") or "")
    paragraph_index = str(redline.get("paragraph_index") or "")
    source_index = redline.get("source_index")
    target_parts = []
    if paragraph_id:
        target_parts.append(paragraph_id)
    if paragraph_index:
        target_parts.append(f"review paragraph {paragraph_index}")
    if source_index is not None:
        target_parts.append(f"source paragraph {source_index}")
    target = " / ".join(target_parts)
    output = [
        _label_value("Action", action),
        _label_value("Target", target or "No paragraph target identified."),
    ]

    if redline.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH:
        output.append(_label_value("Anchor paragraph", redline.get("anchor_text")))
        output.append(_label_value("Insert text", redline.get("insert_text") or redline.get("replacement_text")))
    elif redline.get("action") == REDLINE_DELETE_PARAGRAPH:
        output.append(_label_value("Remove paragraph", redline.get("original_text")))
    else:
        output.append(_label_value("Original paragraph", redline.get("original_text")))
        output.append(_label_value("Replacement text", redline.get("replacement_text")))

    template_options = redline.get("template_options", [])
    if isinstance(template_options, list) and template_options:
        output.append(_paragraph("Template options", style="Heading3"))
        for option in template_options:
            if not isinstance(option, dict):
                continue
            default = " (default)" if option.get("selected") else ""
            label = f"{option.get('label', 'Option')}{default}"
            option_text = option.get("text") or option.get("replacement_text") or option.get("insert_text")
            output.append(_label_value(label, option_text))
    return output


def _redlines_by_clause(redlines: object) -> Dict[str, List[RedlineEdit]]:
    grouped: Dict[str, List[RedlineEdit]] = {}
    if not isinstance(redlines, list):
        return grouped

    for redline in redlines:
        if not isinstance(redline, dict):
            continue
        clause_id = str(redline.get("clause_id", ""))
        grouped.setdefault(clause_id, []).append(redline)
    return grouped


def _redlines_by_paragraph(redlines: object) -> Dict[str, List[RedlineEdit]]:
    grouped: Dict[str, List[RedlineEdit]] = {}
    if not isinstance(redlines, list):
        return grouped

    for redline in redlines:
        if not isinstance(redline, dict):
            continue
        paragraph_id = str(redline.get("paragraph_id", ""))
        grouped.setdefault(paragraph_id, []).append(redline)
    return grouped


def _redlines_by_source_paragraph(
    redlines: object,
    source_paragraphs: List[SourceParagraph],
    review_paragraphs: object = None,
) -> Dict[int, List[RedlineEdit]]:
    grouped: Dict[int, List[RedlineEdit]] = {}
    if not isinstance(redlines, list):
        return grouped

    review_paragraphs_by_id = _review_paragraphs_by_id(review_paragraphs)
    for redline in redlines:
        if not isinstance(redline, dict):
            continue
        source_paragraph = _resolve_source_paragraph(redline, source_paragraphs, review_paragraphs_by_id)
        if source_paragraph is None:
            LOGGER.warning(
                "Skipping source redline with unresolved or ambiguous anchor: id=%s action=%s paragraph_id=%s",
                redline.get("id"),
                redline.get("action"),
                redline.get("paragraph_id"),
            )
            continue
        grouped.setdefault(source_paragraph.source_index, []).append(redline)
    return grouped


def _review_paragraphs_by_id(review_paragraphs: object) -> Dict[str, Paragraph]:
    if not isinstance(review_paragraphs, list):
        return {}
    return {
        str(paragraph.get("id")): paragraph
        for paragraph in review_paragraphs
        if isinstance(paragraph, dict) and paragraph.get("id")
    }


def _resolve_source_paragraph(
    redline: RedlineEdit,
    source_paragraphs: List[SourceParagraph],
    review_paragraphs_by_id: Dict[str, Paragraph],
) -> SourceParagraph | None:
    source_index = _redline_source_index(redline)
    anchor_texts = _redline_anchor_texts(redline, review_paragraphs_by_id)
    for anchor_text in anchor_texts:
        matches = [
            paragraph
            for paragraph in source_paragraphs
            if paragraph.normalized_text == _normalize_paragraph_text(anchor_text)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and source_index is not None:
            indexed_match = next((paragraph for paragraph in matches if paragraph.source_index == source_index), None)
            if indexed_match is not None:
                return indexed_match
        if matches and source_index is None:
            return None

    if source_index is None:
        return None
    return next((paragraph for paragraph in source_paragraphs if paragraph.source_index == source_index), None)


def _redline_source_index(redline: RedlineEdit) -> int | None:
    if redline.get("source_part"):
        return None
    source_index = redline.get("source_index", redline.get("paragraph_index"))
    try:
        return int(source_index)
    except (TypeError, ValueError):
        return None


def _redline_anchor_texts(redline: RedlineEdit, review_paragraphs_by_id: Dict[str, Paragraph]) -> List[str]:
    candidates: List[object] = []
    if redline.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH:
        candidates.extend([redline.get("anchor_text"), redline.get("original_text")])
    else:
        candidates.extend([redline.get("original_text"), redline.get("anchor_text")])

    review_paragraph = review_paragraphs_by_id.get(str(redline.get("paragraph_id") or ""))
    if review_paragraph:
        candidates.append(review_paragraph.get("text"))

    texts: List[str] = []
    seen = set()
    for candidate in candidates:
        normalized = _normalize_paragraph_text(candidate)
        if not normalized or normalized in seen:
            continue
        texts.append(str(candidate or ""))
        seen.add(normalized)
    return texts


def _normalize_paragraph_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _apply_redline_edits_to_source_document(
    document_root: ET.Element,
    redlines: object,
    review_paragraphs: object = None,
) -> None:
    source_paragraphs = _indexed_source_paragraphs(document_root)
    redlines_by_source_index = _redlines_by_source_paragraph(redlines, source_paragraphs, review_paragraphs)
    if not redlines_by_source_index:
        return

    revision_id = _next_revision_id(document_root)
    for source_paragraph in reversed(source_paragraphs):
        edits = redlines_by_source_index.get(source_paragraph.source_index, [])
        if not edits:
            continue

        siblings = list(source_paragraph.parent)
        try:
            paragraph_position = siblings.index(source_paragraph.paragraph)
        except ValueError:
            continue

        primary_edit = next((edit for edit in edits if edit.get("action") != REDLINE_INSERT_AFTER_PARAGRAPH), None)
        if primary_edit and primary_edit.get("action") == REDLINE_REPLACE_PARAGRAPH:
            replacement_paragraph, revision_id = _source_tracked_replace_paragraph(
                source_paragraph.paragraph,
                str(primary_edit.get("original_text") or _paragraph_text(source_paragraph.paragraph)),
                str(primary_edit.get("replacement_text") or ""),
                revision_id,
            )
            source_paragraph.parent[paragraph_position] = replacement_paragraph
        elif primary_edit and primary_edit.get("action") == REDLINE_DELETE_PARAGRAPH:
            source_paragraph.parent[paragraph_position] = _source_tracked_delete_paragraph(
                source_paragraph.paragraph,
                str(primary_edit.get("original_text") or _paragraph_text(source_paragraph.paragraph)),
                revision_id,
            )
            revision_id += 1

        insert_position = paragraph_position + 1
        for insertion in [edit for edit in edits if edit.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH]:
            insert_text = str(insertion.get("insert_text") or insertion.get("replacement_text") or "")
            for inserted_paragraph in _source_tracked_insert_paragraphs(insert_text, revision_id):
                source_paragraph.parent.insert(insert_position, inserted_paragraph)
                insert_position += 1
                revision_id += 1


def _indexed_source_paragraphs(root: ET.Element) -> List[SourceParagraph]:
    paragraphs: List[SourceParagraph] = []
    source_index = 0

    def visit(parent: ET.Element) -> None:
        nonlocal source_index
        for child in list(parent):
            if child.tag == _w_tag("p"):
                source_index += 1
                text = _paragraph_text(child)
                paragraphs.append(
                    SourceParagraph(
                        source_index=source_index,
                        parent=parent,
                        paragraph=child,
                        text=text,
                        normalized_text=_normalize_paragraph_text(text),
                    )
                )
            visit(child)

    visit(root)
    return paragraphs


def _source_tracked_replace_paragraph(
    source_paragraph: ET.Element,
    original: str,
    replacement: str,
    first_revision_id: int,
) -> Tuple[ET.Element, int]:
    tracked_paragraph_xml, next_revision_id = _tracked_replace_paragraph(original, replacement, first_revision_id)
    return _merge_source_paragraph_properties(source_paragraph, _word_paragraph_from_xml(tracked_paragraph_xml)), next_revision_id


def _source_tracked_delete_paragraph(source_paragraph: ET.Element, text: str, revision_id: int) -> ET.Element:
    return _merge_source_paragraph_properties(
        source_paragraph,
        _word_paragraph_from_xml(_tracked_delete_paragraph(text, revision_id)),
    )


def _source_tracked_insert_paragraphs(text: str, first_revision_id: int) -> List[ET.Element]:
    return [
        _word_paragraph_from_xml(paragraph_xml)
        for paragraph_xml in _tracked_insert_paragraphs(text, first_revision_id)
    ]


def _merge_source_paragraph_properties(source_paragraph: ET.Element, tracked_paragraph: ET.Element) -> ET.Element:
    merged = ET.Element(source_paragraph.tag, source_paragraph.attrib)
    source_properties = source_paragraph.find(_w_tag("pPr"))
    tracked_properties = tracked_paragraph.find(_w_tag("pPr"))
    merged_properties = _clone_element(source_properties) if source_properties is not None else None

    if tracked_properties is not None:
        tracked_run_properties = tracked_properties.find(_w_tag("rPr"))
        if tracked_run_properties is not None:
            if merged_properties is None:
                merged_properties = ET.Element(_w_tag("pPr"))
            merged_run_properties = merged_properties.find(_w_tag("rPr"))
            if merged_run_properties is None:
                merged_properties.append(_clone_element(tracked_run_properties))
            else:
                for child in list(tracked_run_properties):
                    merged_run_properties.append(_clone_element(child))

    if merged_properties is not None:
        merged.append(merged_properties)
    for child in list(tracked_paragraph):
        if child.tag != _w_tag("pPr"):
            merged.append(_clone_element(child))
    return merged


def _word_paragraph_from_xml(paragraph_xml: str) -> ET.Element:
    wrapper = ET.fromstring(f'<root xmlns:w="{W_NS}">{paragraph_xml}</root>')
    return wrapper[0]


def _paragraph_text(paragraph: ET.Element) -> str:
    parts = []
    for node in paragraph.iter():
        if node.tag == _w_tag("t") and node.text:
            parts.append(node.text)
        elif node.tag == _w_tag("tab"):
            parts.append("\t")
        elif node.tag in {_w_tag("br"), _w_tag("cr")}:
            parts.append("\n")
    return "".join(parts).strip()


def _label_value(label: str, value: object) -> str:
    text = str(value or "")
    return _paragraph(label, bold=True) + _paragraph(text or "None.")


def _paragraph(text: str, style: str | None = None, bold: bool = False) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{_escape_attr(style)}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}{_run(text, bold=bold)}</w:p>"


def _run(text: str, bold: bool = False) -> str:
    run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    parts = []
    for index, line in enumerate(str(text).split("\n")):
        if index:
            parts.append("<w:br/>")
        parts.append(f'<w:t xml:space="preserve">{_escape_xml(line)}</w:t>')
    return f"<w:r>{run_props}{''.join(parts)}</w:r>"


def _tracked_delete_paragraph(text: str, revision_id: int) -> str:
    revision_attrs = _revision_attrs(revision_id)
    return f"<w:p>{_tracked_delete_with_attrs(text, revision_attrs)}</w:p>"


def _tracked_replace_paragraph(original: str, replacement: str, first_revision_id: int) -> Tuple[str, int]:
    runs: List[str] = []
    revision_id = first_revision_id
    current_type = ""
    current_parts: List[str] = []
    previous_original_token = ""
    previous_accepted_token = ""

    def flush_current() -> None:
        nonlocal revision_id, current_type, current_parts
        if not current_parts:
            return
        text = "".join(current_parts)
        if current_type == "delete":
            runs.append(_tracked_delete(text, revision_id))
            revision_id += 1
        elif current_type == "insert":
            runs.append(_tracked_insert(text, revision_id))
            revision_id += 1
        else:
            runs.append(_run(text))
        current_parts = []

    for operation_type, token in diff_text_operations(original, replacement):
        if operation_type != current_type:
            flush_current()
            current_type = operation_type
        if operation_type == "delete":
            prefix = " " if _needs_inline_space(previous_original_token, token) else ""
            previous_original_token = token
        elif operation_type == "insert":
            prefix = " " if _needs_inline_space(previous_accepted_token, token) else ""
            previous_accepted_token = token
        else:
            prefix = (
                " "
                if _needs_inline_space(previous_original_token, token)
                or _needs_inline_space(previous_accepted_token, token)
                else ""
            )
            previous_original_token = token
            previous_accepted_token = token
        current_parts.append(f"{prefix}{token}")

    flush_current()
    return f"<w:p>{''.join(runs)}</w:p>", revision_id


def _tracked_insert_paragraphs(text: str, first_revision_id: int) -> List[str]:
    blocks = [block for block in str(text).split("\n\n") if block.strip()]
    if not blocks:
        blocks = [str(text)]
    paragraphs: List[str] = []
    for index, block in enumerate(blocks):
        revision_attrs = _revision_attrs(first_revision_id + index)
        paragraphs.append(
            f"<w:p>{_tracked_insert_with_attrs(block, revision_attrs)}</w:p>"
        )
    return paragraphs


def _tracked_delete(text: str, revision_id: int) -> str:
    return _tracked_delete_with_attrs(text, _revision_attrs(revision_id))


def _tracked_insert(text: str, revision_id: int) -> str:
    return _tracked_insert_with_attrs(text, _revision_attrs(revision_id))


def _tracked_delete_with_attrs(text: str, revision_attrs: str) -> str:
    return f'<w:del {revision_attrs}>{_deleted_run(text)}</w:del>'


def _tracked_insert_with_attrs(text: str, revision_attrs: str) -> str:
    return f'<w:ins {revision_attrs}>{_run(text)}</w:ins>'


def _deleted_run(text: str) -> str:
    parts = []
    for index, line in enumerate(str(text).split("\n")):
        if index:
            parts.append("<w:br/>")
        parts.append(f'<w:delText xml:space="preserve">{_escape_xml(line)}</w:delText>')
    return f"<w:r>{''.join(parts)}</w:r>"


def _needs_inline_space(previous_token: str, token: str) -> bool:
    if not previous_token:
        return False
    if re.match(r"^[,.;:!?%)]$", token):
        return False
    if re.match(r"^[(]$", previous_token):
        return False
    return True


def _revision_attrs(revision_id: int) -> str:
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f'w:id="{revision_id}" w:author="nda-automation" w:date="{timestamp}"'


def _status_label(status: str) -> str:
    if status == "meets_requirements":
        return "Meets requirements"
    if status == "does_not_meet_requirements":
        return "Does not meet requirements"
    return status or "Unknown"


def _document_xml(body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    {body_xml}
    <w:sectPr>
      <w:pgSz w:w="{A4_PAGE_WIDTH_TWIPS}" w:h="{A4_PAGE_HEIGHT_TWIPS}"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>"""


def _ensure_document_section_properties(document_root: ET.Element) -> None:
    body = document_root.find(_w_tag("body"))
    if body is None:
        body = ET.SubElement(document_root, _w_tag("body"))
    if body.find(_w_tag("sectPr")) is not None:
        return
    body.append(_default_section_properties())


def _default_section_properties() -> ET.Element:
    section = ET.Element(_w_tag("sectPr"))
    ET.SubElement(section, _w_tag("pgSz"), {
        _w_tag("w"): A4_PAGE_WIDTH_TWIPS,
        _w_tag("h"): A4_PAGE_HEIGHT_TWIPS,
    })
    ET.SubElement(section, _w_tag("pgMar"), {
        _w_tag("top"): "1440",
        _w_tag("right"): "1440",
        _w_tag("bottom"): "1440",
        _w_tag("left"): "1440",
        _w_tag("header"): "720",
        _w_tag("footer"): "720",
        _w_tag("gutter"): "0",
    })
    return section


def _settings_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="{W_NS}">
  <w:revisionView w:markup="1" w:comments="1" w:insDel="1" w:formatting="1" w:inkAnnotations="1"/>
  <w:trackRevisions/>
</w:settings>"""


def _styles_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="140" w:line="276" w:lineRule="auto"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:sz w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:rFonts w:ascii="Aptos Display" w:hAnsi="Aptos Display"/><w:sz w:val="40"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="220"/></w:pPr>
    <w:rPr><w:color w:val="44546A"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="300" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="30"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="240" w:after="100"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="26"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="160" w:after="80"/></w:pPr>
    <w:rPr><w:b/><w:color w:val="5523B2"/><w:sz w:val="23"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Note">
    <w:name w:val="Note"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:after="220"/></w:pPr>
    <w:rPr><w:i/><w:color w:val="44546A"/></w:rPr>
  </w:style>
</w:styles>"""


def _content_types_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="{DOCX_MIME}.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def _package_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _package_rels_xml_with_document(relationships_xml: bytes | None) -> bytes:
    if relationships_xml:
        relationships_root = ET.fromstring(relationships_xml)
    else:
        relationships_root = ET.Element(_rel_tag("Relationships"))

    has_document = any(
        relationship.attrib.get("Type") == OFFICE_DOCUMENT_RELATIONSHIP_TYPE
        for relationship in relationships_root.findall(_rel_tag("Relationship"))
    )
    if not has_document:
        ET.SubElement(relationships_root, _rel_tag("Relationship"), {
            "Id": _next_relationship_id(relationships_root),
            "Type": OFFICE_DOCUMENT_RELATIONSHIP_TYPE,
            "Target": "word/document.xml",
        })
    return _xml_bytes(relationships_root)


def _document_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
</Relationships>"""


def _settings_xml_with_track_revisions(settings_xml: bytes | None) -> bytes:
    if settings_xml:
        settings_root = ET.fromstring(settings_xml)
    else:
        settings_root = ET.Element(_w_tag("settings"))

    revision_view = settings_root.find(_w_tag("revisionView"))
    if revision_view is None:
        settings_root.insert(0, ET.Element(_w_tag("revisionView"), {
            _w_tag("markup"): "1",
            _w_tag("comments"): "1",
            _w_tag("insDel"): "1",
            _w_tag("formatting"): "1",
            _w_tag("inkAnnotations"): "1",
        }))

    track_revisions = settings_root.find(_w_tag("trackRevisions"))
    if track_revisions is None:
        settings_root.insert(0, ET.Element(_w_tag("trackRevisions")))
    else:
        track_revisions.attrib.pop(_w_tag("val"), None)
        track_revisions.attrib.pop("val", None)
    return _xml_bytes(settings_root)


def _document_rels_xml_with_settings(relationships_xml: bytes | None) -> bytes:
    if relationships_xml:
        relationships_root = ET.fromstring(relationships_xml)
    else:
        relationships_root = ET.Element(_rel_tag("Relationships"))

    has_settings = any(
        relationship.attrib.get("Type") == SETTINGS_RELATIONSHIP_TYPE
        for relationship in relationships_root.findall(_rel_tag("Relationship"))
    )
    if not has_settings:
        ET.SubElement(relationships_root, _rel_tag("Relationship"), {
            "Id": _next_relationship_id(relationships_root),
            "Type": SETTINGS_RELATIONSHIP_TYPE,
            "Target": "settings.xml",
        })
    return _xml_bytes(relationships_root)


def _content_types_xml_with_settings(content_types_xml: bytes | None) -> bytes:
    if content_types_xml:
        content_types_root = ET.fromstring(content_types_xml)
    else:
        content_types_root = ET.Element(_content_type_tag("Types"))
        ET.SubElement(content_types_root, _content_type_tag("Default"), {
            "Extension": "rels",
            "ContentType": "application/vnd.openxmlformats-package.relationships+xml",
        })
        ET.SubElement(content_types_root, _content_type_tag("Default"), {
            "Extension": "xml",
            "ContentType": "application/xml",
        })
        ET.SubElement(content_types_root, _content_type_tag("Override"), {
            "PartName": "/word/document.xml",
            "ContentType": f"{DOCX_MIME}.main+xml",
        })

    has_settings = any(
        override.attrib.get("PartName") == "/word/settings.xml"
        for override in content_types_root.findall(_content_type_tag("Override"))
    )
    if not has_settings:
        ET.SubElement(content_types_root, _content_type_tag("Override"), {
            "PartName": "/word/settings.xml",
            "ContentType": SETTINGS_CONTENT_TYPE,
        })
    return _xml_bytes(content_types_root)


def _core_properties_xml(title: str) -> str:
    created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:dcmitype="http://purl.org/dc/dcmitype/"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{_escape_xml(title)} redline report</dc:title>
  <dc:creator>nda-automation</dc:creator>
  <cp:lastModifiedBy>nda-automation</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>
</cp:coreProperties>"""


def _app_properties_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
  xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>nda-automation</Application>
</Properties>"""


def _escape_xml(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _escape_attr(value: str) -> str:
    return _escape_xml(value)


def _w_tag(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _rel_tag(tag: str) -> str:
    return f"{{{REL_NS}}}{tag}"


def _content_type_tag(tag: str) -> str:
    return f"{{{CONTENT_TYPES_NS}}}{tag}"


def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _clone_element(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="utf-8"))


def _next_revision_id(root: ET.Element) -> int:
    revision_ids = []
    for element in root.iter():
        value = element.attrib.get(_w_tag("id"))
        if value is None:
            continue
        try:
            revision_ids.append(int(value))
        except ValueError:
            continue
    return max(revision_ids, default=0) + 1


def _next_relationship_id(relationships_root: ET.Element) -> str:
    relationship_numbers = []
    for relationship in relationships_root.findall(_rel_tag("Relationship")):
        relationship_id = relationship.attrib.get("Id", "")
        match = re.fullmatch(r"rId(\d+)", relationship_id)
        if match:
            relationship_numbers.append(int(match.group(1)))
    return f"rId{max(relationship_numbers, default=0) + 1}"
