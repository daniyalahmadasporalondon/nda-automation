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
from .docx_health import validate_docx_open_health as validate_docx_open_health
from .docx_text import DocxExtractionError, validate_docx_archive, validate_docx_bytes_before_open
from .docx_comments import _apply_comment_anchor, _comments_xml_with_appended_comments
from .redline_xml import (
    _run,
    _source_tracked_delete_paragraph,
    _source_tracked_insert_paragraphs,
    _source_tracked_replace_paragraph,
    _strip_paragraph_property_revisions,
    _tracked_delete_paragraph,
    _tracked_insert_paragraphs,
    _tracked_replace_paragraph,
)
from .docx_xml import (
    UnsafeDocxXmlError,
    W_NS,
    _content_type_tag,
    _escape_attr,
    _escape_xml,
    _normalize_paragraph_text,
    _paragraph_text,
    _parse_docx_xml_with_namespaces,
    _rel_tag,
    _w_tag,
    _xml_bytes,
    parse_docx_xml,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SETTINGS_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
COMMENTS_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
OFFICE_DOCUMENT_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
SETTINGS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
COMMENTS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
RELATIONSHIPS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
STYLES_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
A4_PAGE_WIDTH_TWIPS = "11906"
A4_PAGE_HEIGHT_TWIPS = "16838"

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
        _paragraph(f"Requirements needing review: {review_result.get('requirements_needs_review', 0)}"),
        _paragraph(f"Requirements failed: {review_result.get('requirements_failed', 0)}"),
        _paragraph(f"Checked at: {checked_at}"),
        _paragraph("Clause Findings", style="Heading1"),
    ]

    redlines_by_clause = _redlines_by_clause(review_result.get("redline_edits", []))
    for clause in review_result.get("clauses", []):
        if not isinstance(clause, dict):
            continue
        paragraphs.extend(_clause_section(clause, redlines_by_clause.get(str(clause.get("id")), [])))

    document_root = parse_docx_xml(_document_xml("".join(paragraphs)), part_name="word/document.xml")
    report_comments = _targeted_report_comments(review_result)
    assigned_comments, comments_xml = _comments_xml_with_appended_comments(None, report_comments)
    if assigned_comments:
        _apply_comment_anchors_to_report_document(
            document_root,
            assigned_comments,
            review_result.get("paragraphs", []),
        )
    document_xml = _xml_bytes(document_root)

    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _content_types_xml(include_comments=bool(assigned_comments)))
            archive.writestr("_rels/.rels", _package_rels_xml())
            archive.writestr("docProps/core.xml", _core_properties_xml(title or "NDA Review"))
            archive.writestr("docProps/app.xml", _app_properties_xml())
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/styles.xml", _styles_xml())
            archive.writestr("word/settings.xml", _settings_xml())
            archive.writestr("word/_rels/document.xml.rels", _document_rels_xml(include_comments=bool(assigned_comments)))
            if assigned_comments:
                archive.writestr("word/comments.xml", comments_xml)
        return output.getvalue()


def build_source_redline_docx(source_docx: bytes, review_result: ReviewResult) -> bytes:
    try:
        validate_docx_bytes_before_open(source_docx)
        with ZipFile(BytesIO(source_docx), "r") as source_archive:
            validate_docx_archive(source_archive)
            source_names = set(source_archive.namelist())
            document_xml = source_archive.read("word/document.xml")
            document_root, document_namespaces = _parse_docx_xml_with_namespaces(
                document_xml,
                part_name="word/document.xml",
            )
            _strip_paragraph_property_revisions(document_root)
            _apply_redline_edits_to_source_document(
                document_root,
                review_result.get("redline_edits", []),
                review_result.get("paragraphs", []),
            )
            source_comments = _targeted_source_comments(review_result, document_root)
            assigned_comments, comments_xml = _comments_xml_with_appended_comments(
                source_archive.read("word/comments.xml") if "word/comments.xml" in source_names else None,
                source_comments,
            )
            if assigned_comments:
                _apply_comment_anchors_to_source_document(document_root, assigned_comments)
            _ensure_document_section_properties(document_root)

            overrides: Dict[str, bytes] = {
                "word/document.xml": _xml_bytes(document_root, namespace_declarations=document_namespaces),
                "word/settings.xml": _settings_xml_with_track_revisions(
                    source_archive.read("word/settings.xml") if "word/settings.xml" in source_names else None
                ),
                "word/_rels/document.xml.rels": _document_rels_xml_with_settings(
                    source_archive.read("word/_rels/document.xml.rels")
                    if "word/_rels/document.xml.rels" in source_names
                    else None,
                    has_comments=bool(assigned_comments),
                ),
                "[Content_Types].xml": _content_types_xml_with_settings(
                    source_archive.read("[Content_Types].xml") if "[Content_Types].xml" in source_names else None,
                    has_styles="word/styles.xml" in source_names,
                    has_comments=bool(assigned_comments),
                ),
                "_rels/.rels": _package_rels_xml_with_document(
                    source_archive.read("_rels/.rels") if "_rels/.rels" in source_names else None
                ),
            }
            if assigned_comments:
                overrides["word/comments.xml"] = comments_xml

            with BytesIO() as output:
                with ZipFile(output, "w", ZIP_DEFLATED) as redlined_archive:
                    written = set()
                    for item in source_archive.infolist():
                        if item.filename in written:
                            continue
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
    except (BadZipFile, DocxExtractionError, KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
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
    status = _clause_decision_label(clause)
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
) -> Tuple[Dict[int, List[RedlineEdit]], List[RedlineEdit]]:
    grouped: Dict[int, List[RedlineEdit]] = {}
    unresolved: List[RedlineEdit] = []
    if not isinstance(redlines, list):
        return grouped, unresolved

    review_paragraphs_by_id = _review_paragraphs_by_id(review_paragraphs)
    # Resolve one source paragraph per *distinct review paragraph*, claiming each
    # physical <w:p> at most once. Two review paragraphs that split one extracted
    # block on an internal blank line share a provenance source_index but are
    # distinct review paragraphs; their redlines must land on distinct source
    # paragraphs instead of colliding on the single physical paragraph whose ordinal
    # happens to equal source_index. Multiple redlines for the *same* review
    # paragraph still resolve to (and reuse) that paragraph's one source <w:p>.
    claimed_indexes: set[int] = set()
    resolved_by_review_key: Dict[Tuple, SourceParagraph] = {}
    for redline in sorted(
        (redline for redline in redlines if isinstance(redline, dict)),
        key=_redline_resolution_order,
    ):
        review_key = _redline_review_paragraph_key(redline)
        source_paragraph = resolved_by_review_key.get(review_key) if review_key is not None else None
        if source_paragraph is None:
            source_paragraph = _resolve_source_paragraph(
                redline, source_paragraphs, review_paragraphs_by_id, claimed_indexes=claimed_indexes
            )
            if source_paragraph is None:
                if not _redline_source_part(redline, review_paragraphs_by_id):
                    unresolved.append(redline)
                    continue
                LOGGER.warning(
                    "Skipping source redline with unresolved or ambiguous anchor: id=%s action=%s paragraph_id=%s",
                    redline.get("id"),
                    redline.get("action"),
                    redline.get("paragraph_id"),
                )
                continue
            claimed_indexes.add(source_paragraph.source_index)
            if review_key is not None:
                resolved_by_review_key[review_key] = source_paragraph
        grouped.setdefault(source_paragraph.source_index, []).append(redline)
    return grouped, unresolved


def _redline_review_paragraph_key(redline: RedlineEdit) -> Tuple | None:
    """Identity of the review paragraph a redline targets, so multiple edits on one
    paragraph share its resolved source paragraph. Falls back to the provenance
    source_index when no paragraph_id is present."""
    paragraph_id = str(redline.get("paragraph_id") or "").strip()
    if paragraph_id:
        return ("paragraph_id", paragraph_id)
    source_index = _redline_source_index(redline)
    if source_index is not None:
        return ("source_index", source_index)
    return None


def _redline_resolution_order(redline: RedlineEdit) -> Tuple[int, int]:
    """Document order for one-to-one anchor claiming: the unique review paragraph
    index (then source_index) so earlier review paragraphs claim earlier matches."""
    paragraph_index = redline.get("paragraph_index")
    source_index = _redline_source_index(redline)
    return (
        paragraph_index if isinstance(paragraph_index, int) else 1_000_000,
        source_index if isinstance(source_index, int) else 1_000_000,
    )


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
    *,
    claimed_indexes: set[int] | None = None,
) -> SourceParagraph | None:
    if _redline_source_part(redline, review_paragraphs_by_id):
        return None
    claimed_indexes = claimed_indexes if claimed_indexes is not None else set()
    source_index = _redline_source_index(redline)
    anchor_texts = _redline_anchor_texts(redline, review_paragraphs_by_id)
    for anchor_text in anchor_texts:
        matches = [
            paragraph
            for paragraph in source_paragraphs
            if paragraph.normalized_text == _normalize_paragraph_text(anchor_text)
        ]
        if len(matches) == 1:
            # Unambiguous text match. Only decline it if a *different* review
            # paragraph already claimed it (the split-on-blank-line collision);
            # otherwise behave exactly as before.
            return matches[0] if matches[0].source_index not in claimed_indexes else None
        if len(matches) > 1:
            unclaimed = [paragraph for paragraph in matches if paragraph.source_index not in claimed_indexes]
            if source_index is not None:
                indexed_match = next(
                    (paragraph for paragraph in unclaimed if paragraph.source_index == source_index),
                    None,
                )
                if indexed_match is not None:
                    return indexed_match
                # source_index points at an already-claimed (or absent) match: take
                # the earliest still-unclaimed same-text paragraph in document order
                # so split/duplicate blocks resolve one-to-one.
                if unclaimed:
                    return unclaimed[0]
            else:
                # No source_index hint: an ambiguous text anchor is unresolvable,
                # preserving the prior reject-ambiguous-without-index contract.
                return None

    if source_index is None:
        return None
    return next(
        (
            paragraph
            for paragraph in source_paragraphs
            if paragraph.source_index == source_index and paragraph.source_index not in claimed_indexes
        ),
        None,
    )


def _redline_source_index(redline: RedlineEdit) -> int | None:
    if redline.get("source_part"):
        return None
    source_index = redline.get("source_index", redline.get("paragraph_index"))
    try:
        return int(source_index)
    except (TypeError, ValueError):
        return None


def _redline_source_part(redline: RedlineEdit, review_paragraphs_by_id: Dict[str, Paragraph]) -> str:
    source_part = str(redline.get("source_part") or "").strip()
    if source_part:
        return source_part
    review_paragraph = review_paragraphs_by_id.get(str(redline.get("paragraph_id") or ""))
    if isinstance(review_paragraph, dict):
        return str(review_paragraph.get("source_part") or "").strip()
    return ""


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


def _apply_redline_edits_to_source_document(
    document_root: ET.Element,
    redlines: object,
    review_paragraphs: object = None,
) -> None:
    source_paragraphs = _indexed_source_paragraphs(document_root)
    redlines_by_source_index, unresolved_redlines = _redlines_by_source_paragraph(
        redlines,
        source_paragraphs,
        review_paragraphs,
    )
    if unresolved_redlines:
        raise DocxExportError(_unanchored_redline_error(unresolved_redlines))
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

        primary_edits = [edit for edit in edits if edit.get("action") != REDLINE_INSERT_AFTER_PARAGRAPH]
        insert_position = paragraph_position + 1
        primary_applied = False
        for primary_edit in primary_edits:
            primary_paragraph, revision_id = _source_tracked_primary_redline_paragraph(
                source_paragraph.paragraph,
                primary_edit,
                revision_id,
            )
            if primary_paragraph is None:
                continue
            if primary_applied:
                source_paragraph.parent.insert(insert_position, primary_paragraph)
                insert_position += 1
            else:
                source_paragraph.parent[paragraph_position] = primary_paragraph
                primary_applied = True

        for insertion in [edit for edit in edits if edit.get("action") == REDLINE_INSERT_AFTER_PARAGRAPH]:
            insert_text = str(insertion.get("insert_text") or insertion.get("replacement_text") or "")
            for inserted_paragraph in _source_tracked_insert_paragraphs(insert_text, revision_id):
                source_paragraph.parent.insert(insert_position, inserted_paragraph)
                insert_position += 1
                revision_id += 1


def _unanchored_redline_error(redlines: List[RedlineEdit]) -> str:
    count = len(redlines)
    plural = "" if count == 1 else "s"
    ids = [
        str(redline.get("id") or redline.get("clause_id") or redline.get("paragraph_id") or "").strip()
        for redline in redlines[:5]
    ]
    visible_ids = [redline_id for redline_id in ids if redline_id]
    suffix = f" ({', '.join(visible_ids)})" if visible_ids else ""
    return (
        f"The uploaded Word document could not anchor {count} approved redline{plural}{suffix}. "
        "Re-run the review or add source paragraph indexes before exporting."
    )


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
                continue
            visit(child)

    visit(root)
    return paragraphs


def _source_tracked_primary_redline_paragraph(
    source_paragraph: ET.Element,
    redline: RedlineEdit,
    revision_id: int,
) -> Tuple[ET.Element | None, int]:
    original_text = str(redline.get("original_text") or _paragraph_text(source_paragraph))
    if redline.get("action") == REDLINE_REPLACE_PARAGRAPH:
        return _source_tracked_replace_paragraph(
            source_paragraph,
            original_text,
            str(redline.get("replacement_text") or ""),
            revision_id,
        )
    if redline.get("action") == REDLINE_DELETE_PARAGRAPH:
        return _source_tracked_delete_paragraph(source_paragraph, original_text, revision_id), revision_id + 1
    return None, revision_id


def _targeted_report_comments(review_result: ReviewResult) -> List[dict]:
    comments = _prepared_review_comments(review_result)
    review_paragraphs = _review_paragraphs_by_id(review_result.get("paragraphs", []))
    targeted: List[dict] = []
    for comment in comments:
        paragraph_id = str(comment.get("paragraph_id") or "")
        if paragraph_id not in review_paragraphs:
            continue
        targeted.append({
            **comment,
            "_report_paragraph_id": paragraph_id,
        })
    return targeted


def _targeted_source_comments(review_result: ReviewResult, document_root: ET.Element) -> List[dict]:
    comments = _prepared_review_comments(review_result)
    review_paragraphs = _review_paragraphs_by_id(review_result.get("paragraphs", []))
    source_paragraph_indexes = {paragraph.source_index for paragraph in _indexed_source_paragraphs(document_root)}
    targeted: List[dict] = []
    for comment in comments:
        source_index = _comment_source_index(comment, review_paragraphs)
        if source_index is None or source_index not in source_paragraph_indexes:
            continue
        targeted.append({
            **comment,
            "_source_index": source_index,
        })
    return targeted


def _prepared_review_comments(review_result: ReviewResult) -> List[dict]:
    comments = review_result.get("review_comments", [])
    if not isinstance(comments, list):
        return []

    prepared: List[dict] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        text = str(comment.get("text") or "").strip()
        if not text:
            continue
        paragraph_id = str(comment.get("paragraph_id") or "").strip()
        if not paragraph_id:
            paragraph_id = _comment_paragraph_id_from_clause(review_result, str(comment.get("clause_id") or ""))
        if not paragraph_id:
            continue
        prepared.append({
            "author": str(comment.get("author") or "Reviewer").strip() or "Reviewer",
            "clause_id": str(comment.get("clause_id") or "").strip(),
            "clause_name": str(comment.get("clause_name") or "").strip(),
            "created_at": str(comment.get("created_at") or "").strip(),
            "id": str(comment.get("id") or "").strip(),
            "paragraph_id": paragraph_id,
            "scope": str(comment.get("scope") or "").strip(),
            "selected_text": str(comment.get("selected_text") or "").strip(),
            "selection_start": comment.get("selection_start"),
            "selection_end": comment.get("selection_end"),
            "text": text,
        })
    return prepared


def _comment_paragraph_id_from_clause(review_result: ReviewResult, clause_id: str) -> str:
    if not clause_id:
        return ""
    for clause in review_result.get("clauses", []):
        if not isinstance(clause, dict) or str(clause.get("id") or "") != clause_id:
            continue
        matched_paragraph_ids = clause.get("matched_paragraph_ids")
        if isinstance(matched_paragraph_ids, list):
            for paragraph_id in matched_paragraph_ids:
                paragraph_id = str(paragraph_id or "").strip()
                if paragraph_id:
                    return paragraph_id
    for redline in review_result.get("redline_edits", []):
        if not isinstance(redline, dict) or str(redline.get("clause_id") or "") != clause_id:
            continue
        paragraph_id = str(redline.get("paragraph_id") or "").strip()
        if paragraph_id:
            return paragraph_id
    return ""


def _comment_source_index(comment: dict, review_paragraphs_by_id: Dict[str, dict]) -> int | None:
    paragraph = review_paragraphs_by_id.get(str(comment.get("paragraph_id") or ""))
    if not paragraph:
        return None
    for key in ("source_index", "index"):
        try:
            value = int(paragraph.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _apply_comment_anchors_to_report_document(
    document_root: ET.Element,
    comments: List[dict],
    review_paragraphs: object,
) -> None:
    body = document_root.find(_w_tag("body"))
    if body is None:
        return
    body_paragraphs = [child for child in list(body) if child.tag == _w_tag("p")]
    if len(body_paragraphs) < 5 or not isinstance(review_paragraphs, list):
        return
    report_paragraph_by_id = {}
    report_paragraph_index = 4
    for paragraph in review_paragraphs:
        if not isinstance(paragraph, dict):
            continue
        if report_paragraph_index >= len(body_paragraphs):
            break
        expected_text = _normalize_paragraph_text(str(paragraph.get("text") or ""))
        while report_paragraph_index < len(body_paragraphs):
            candidate_text = _normalize_paragraph_text(_paragraph_text(body_paragraphs[report_paragraph_index]))
            if not expected_text or expected_text in candidate_text or candidate_text in expected_text:
                break
            report_paragraph_index += 1
        if report_paragraph_index >= len(body_paragraphs):
            break
        paragraph_id = str(paragraph.get("id") or "")
        if paragraph_id:
            report_paragraph_by_id[paragraph_id] = body_paragraphs[report_paragraph_index]
        report_paragraph_index += 1
    for comment in comments:
        paragraph = report_paragraph_by_id.get(str(comment.get("_report_paragraph_id") or ""))
        if paragraph is not None:
            _apply_comment_anchor(paragraph, comment)


def _apply_comment_anchors_to_source_document(document_root: ET.Element, comments: List[dict]) -> None:
    source_paragraph_by_index = {
        paragraph.source_index: paragraph.paragraph
        for paragraph in _indexed_source_paragraphs(document_root)
    }
    for comment in comments:
        paragraph = source_paragraph_by_index.get(comment.get("_source_index"))
        if paragraph is not None:
            _apply_comment_anchor(paragraph, comment)


def _label_value(label: str, value: object) -> str:
    text = str(value or "")
    return _paragraph(label, bold=True) + _paragraph(text or "None.")


def _paragraph(text: str, style: str | None = None, bold: bool = False) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{_escape_attr(style)}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}{_run(text, bold=bold)}</w:p>"


def _status_label(status: str) -> str:
    if status == "meets_requirements":
        return "Meets requirements"
    if status == "needs_review":
        return "Needs review"
    if status == "does_not_meet_requirements":
        return "Does not meet requirements"
    return status or "Unknown"


def _clause_decision_label(clause: ClauseResult) -> str:
    decision = str(clause.get("decision") or "").strip().lower()
    if decision == "review" or clause.get("needs_review"):
        return "REVIEW"
    if decision == "fail":
        return "CHECK"
    if decision == "pass":
        return "PASS"
    return "PASS" if clause.get("passes") else "CHECK"


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


def _content_types_xml(*, include_comments: bool = False) -> str:
    comments_override = (
        f'  <Override PartName="/word/comments.xml" ContentType="{COMMENTS_CONTENT_TYPE}"/>\n'
        if include_comments else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="{DOCX_MIME}.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
{comments_override.rstrip()}
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
        relationships_root, namespaces = _parse_docx_xml_with_namespaces(relationships_xml, part_name="_rels/.rels")
    else:
        relationships_root = ET.Element(_rel_tag("Relationships"))
        namespaces = {}

    _ensure_relationship_target(relationships_root, OFFICE_DOCUMENT_RELATIONSHIP_TYPE, "word/document.xml")
    return _xml_bytes(relationships_root, namespace_declarations=namespaces)


def _document_rels_xml(*, include_comments: bool = False) -> str:
    comments_relationship = (
        f'  <Relationship Id="rId3" Type="{COMMENTS_RELATIONSHIP_TYPE}" Target="comments.xml"/>\n'
        if include_comments else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
{comments_relationship.rstrip()}
</Relationships>"""


def _settings_xml_with_track_revisions(settings_xml: bytes | None) -> bytes:
    if settings_xml:
        settings_root, namespaces = _parse_docx_xml_with_namespaces(settings_xml, part_name="word/settings.xml")
    else:
        settings_root = ET.Element(_w_tag("settings"))
        namespaces = {}

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
    return _xml_bytes(settings_root, namespace_declarations=namespaces)


def _document_rels_xml_with_settings(relationships_xml: bytes | None, *, has_comments: bool = False) -> bytes:
    if relationships_xml:
        relationships_root, namespaces = _parse_docx_xml_with_namespaces(
            relationships_xml,
            part_name="word/_rels/document.xml.rels",
        )
    else:
        relationships_root = ET.Element(_rel_tag("Relationships"))
        namespaces = {}

    _ensure_relationship_target(relationships_root, SETTINGS_RELATIONSHIP_TYPE, "settings.xml")
    if has_comments:
        _ensure_relationship_target(relationships_root, COMMENTS_RELATIONSHIP_TYPE, "comments.xml")
    return _xml_bytes(relationships_root, namespace_declarations=namespaces)


def _content_types_xml_with_settings(
    content_types_xml: bytes | None,
    *,
    has_styles: bool = False,
    has_comments: bool = False,
) -> bytes:
    if content_types_xml:
        content_types_root, namespaces = _parse_docx_xml_with_namespaces(
            content_types_xml,
            part_name="[Content_Types].xml",
        )
    else:
        content_types_root = ET.Element(_content_type_tag("Types"))
        namespaces = {}

    _ensure_content_type_default(content_types_root, "rels", RELATIONSHIPS_CONTENT_TYPE)
    _ensure_content_type_default(content_types_root, "xml", "application/xml")
    _ensure_content_type_override(content_types_root, "/word/document.xml", f"{DOCX_MIME}.main+xml")
    _ensure_content_type_override(content_types_root, "/word/settings.xml", SETTINGS_CONTENT_TYPE)
    if has_styles:
        _ensure_content_type_override(content_types_root, "/word/styles.xml", STYLES_CONTENT_TYPE)
    if has_comments:
        _ensure_content_type_override(content_types_root, "/word/comments.xml", COMMENTS_CONTENT_TYPE)
    return _xml_bytes(content_types_root, namespace_declarations=namespaces)


def _ensure_relationship_target(relationships_root: ET.Element, relationship_type: str, target: str) -> None:
    matches = [
        relationship
        for relationship in relationships_root.findall(_rel_tag("Relationship"))
        if relationship.attrib.get("Type") == relationship_type
    ]
    if not matches:
        ET.SubElement(relationships_root, _rel_tag("Relationship"), {
            "Id": _next_relationship_id(relationships_root),
            "Type": relationship_type,
            "Target": target,
        })
        return

    primary = matches[0]
    primary.attrib["Target"] = target
    primary.attrib.pop("TargetMode", None)
    for duplicate in matches[1:]:
        relationships_root.remove(duplicate)


def _ensure_content_type_default(content_types_root: ET.Element, extension: str, content_type: str) -> None:
    matches = [
        default
        for default in content_types_root.findall(_content_type_tag("Default"))
        if default.attrib.get("Extension") == extension
    ]
    if not matches:
        ET.SubElement(content_types_root, _content_type_tag("Default"), {
            "Extension": extension,
            "ContentType": content_type,
        })
        return

    primary = matches[0]
    primary.attrib["ContentType"] = content_type
    for duplicate in matches[1:]:
        content_types_root.remove(duplicate)


def _ensure_content_type_override(content_types_root: ET.Element, part_name: str, content_type: str) -> None:
    matches = [
        override
        for override in content_types_root.findall(_content_type_tag("Override"))
        if override.attrib.get("PartName") == part_name
    ]
    if not matches:
        ET.SubElement(content_types_root, _content_type_tag("Override"), {
            "PartName": part_name,
            "ContentType": content_type,
        })
        return

    primary = matches[0]
    primary.attrib["ContentType"] = content_type
    for duplicate in matches[1:]:
        content_types_root.remove(duplicate)


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
