from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, List, NamedTuple, Tuple
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from . import redline_edit_contract
from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_FORMAT_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .docx_health import validate_docx_open_health as validate_docx_open_health
from .docx_text import DocxExtractionError, validate_docx_archive, validate_docx_bytes_before_open
from .docx_comments import (
    COMMENTS_EXTENDED_CONTENT_TYPE,
    COMMENTS_EXTENDED_RELATIONSHIP_TYPE,
    _apply_comment_anchor,
    _comments_extended_xml_for_assigned,
    _comments_xml_with_appended_comments,
)
from .redline_xml import (
    _apply_tracked_paragraph_format,
    _apply_tracked_run_format,
    _run,
    _source_tracked_delete_paragraph,
    _source_tracked_insert_paragraphs,
    _source_tracked_replace_paragraph,
    _source_tracked_replace_paragraph_char,
    _source_tracked_replace_paragraph_runs,
    _source_verbatim_paragraph,
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


class SourceRedlinePackage(NamedTuple):
    """The rendered source-redline DOCX bytes plus the PDF-source redlines that
    could not be confidently anchored. ``anchor_uncertain_redlines`` is always empty
    in strict (fail-closed) mode -- strict raises instead of returning them. It is
    only populated in lenient mode (preview/draft/diagnostic), where the file is
    still produced but must be labelled an incomplete redline."""

    data: bytes
    anchor_uncertain_redlines: List[RedlineEdit]


class DocxExportError(ValueError):
    """Raised when a DOCX cannot be patched into a redlined export."""


class PdfRedlineAnchorError(DocxExportError):
    """Raised (in strict/fail-closed mode) when one or more PDF-source redlines
    cannot be confidently anchored into the reconstructed Word body.

    PDF review paragraphs carry only the engine-independent text and a loose,
    unreliable paragraph index, so a redline is placed only when exactly one
    reconstructed body paragraph matches its text within a high confidence. When
    any required redline cannot be placed, producing the file would silently drop
    accepted changes (the original P0 defect); strict mode raises instead. The
    caller (``redline_export_service``) translates this into the user-facing
    ``PdfSourceRedlineUnavailableError`` and offers the source-PDF marked-up
    annotation export (``annotated_pdf_export``) as the recovery path.

    ``count`` is the number of unplaceable redlines (for the exact user message);
    ``redlines`` are those edits (for diagnostics/headers).
    """

    def __init__(self, redlines: List[RedlineEdit]):
        self.redlines = list(redlines)
        self.count = len(self.redlines)
        super().__init__(
            f"{self.count} proposed change(s) could not be confidently placed in the "
            "reconstructed Word document."
        )


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
    comments_extended_xml = _comments_extended_xml_for_assigned(None, assigned_comments) if assigned_comments else b""
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
                archive.writestr("word/commentsExtended.xml", comments_extended_xml)
        return output.getvalue()


def build_source_redline_docx(
    source_docx: bytes,
    review_result: ReviewResult,
    *,
    clean_fills: object = None,
    strict: bool = True,
) -> bytes:
    from .source_redline_docx import build_source_redline_docx as build_source_redline_docx_facade  # noqa: PLC0415

    return build_source_redline_docx_facade(
        source_docx, review_result, clean_fills=clean_fills, strict=strict
    )


def _build_source_redline_docx_package(
    source_docx: bytes,
    review_result: ReviewResult,
    *,
    clean_fills: object = None,
    strict: bool = True,
) -> SourceRedlinePackage:
    """Build the redlined source DOCX.

    ``clean_fills`` (optional) are inbound-NDA clean fills: blank-replacements
    baked into the base document as REAL text BEFORE any tracked redline is
    applied, so they become part of the source text (no tracked-change markup).
    See :mod:`nda_automation.fill_export`. They are validated/sanitised by the
    caller and applied here against the same freshly-parsed ``document_root`` the
    redlines use, so clean fills and redlines agree on the source paragraph model.
    """
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
            if clean_fills:
                # Bake clean fills into the base text FIRST, then apply tracked
                # redlines on top so they layer over the filled document.
                from .fill_export import apply_clean_fills_to_source_document  # noqa: PLC0415

                apply_clean_fills_to_source_document(document_root, list(clean_fills), review_result)
            source_paragraph_by_original_index = _targetable_source_paragraphs_by_index(document_root)
            source_comments = _targeted_source_comments(
                review_result,
                source_paragraph_by_original_index,
            )
            source_paragraph_by_final_index, anchor_uncertain_redlines = _apply_redline_edits_to_source_document(
                document_root,
                review_result.get("redline_edits", []),
                review_result.get("paragraphs", []),
                strict=strict,
            )
            assigned_comments, comments_xml = _comments_xml_with_appended_comments(
                source_archive.read("word/comments.xml") if "word/comments.xml" in source_names else None,
                source_comments,
            )
            comments_extended_xml = (
                _comments_extended_xml_for_assigned(
                    source_archive.read("word/commentsExtended.xml")
                    if "word/commentsExtended.xml" in source_names
                    else None,
                    assigned_comments,
                )
                if assigned_comments
                else b""
            )
            if assigned_comments:
                _apply_comment_anchors_to_source_document(
                    source_paragraph_by_final_index,
                    assigned_comments,
                )
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
                overrides["word/commentsExtended.xml"] = comments_extended_xml

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
                return SourceRedlinePackage(
                    data=output.getvalue(),
                    anchor_uncertain_redlines=anchor_uncertain_redlines,
                )
    except (BadZipFile, DocxExtractionError, KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
        raise DocxExportError("The uploaded Word document could not be redlined.") from exc


def accept_all_revisions(docx_bytes: bytes) -> bytes:
    """Return ``docx_bytes`` with every tracked change ACCEPTED, yielding a clean
    document with no revision markup: ``<w:ins>`` is unwrapped (the insertion is
    kept), ``<w:del>`` is removed (the deletion is applied), ``<w:rPrChange>`` /
    ``<w:pPrChange>`` are dropped (the NEW formatting wins), and the track-revisions
    / revisionView settings are cleared. Used to bake the Generator's edits into a
    clean outbound NDA -- the recipient gets a finished document, not a redline."""
    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes), "r") as source_archive:
            validate_docx_archive(source_archive)
            source_names = set(source_archive.namelist())
            overrides: Dict[str, bytes] = {}

            document_root, document_namespaces = _parse_docx_xml_with_namespaces(
                source_archive.read("word/document.xml"),
                part_name="word/document.xml",
            )
            _accept_revisions_element(document_root)
            overrides["word/document.xml"] = _xml_bytes(
                document_root, namespace_declarations=document_namespaces
            )

            if "word/settings.xml" in source_names:
                settings_root, settings_namespaces = _parse_docx_xml_with_namespaces(
                    source_archive.read("word/settings.xml"),
                    part_name="word/settings.xml",
                )
                for tag in ("trackRevisions", "revisionView"):
                    for node in list(settings_root.findall(_w_tag(tag))):
                        settings_root.remove(node)
                overrides["word/settings.xml"] = _xml_bytes(
                    settings_root, namespace_declarations=settings_namespaces
                )

            with BytesIO() as output:
                with ZipFile(output, "w", ZIP_DEFLATED) as clean_archive:
                    written: set[str] = set()
                    for item in source_archive.infolist():
                        if item.filename in written:
                            continue
                        data = overrides.pop(item.filename, None)
                        if data is None:
                            data = source_archive.read(item.filename)
                        clean_archive.writestr(item, data)
                        written.add(item.filename)
                    for name, data in overrides.items():
                        if name not in written:
                            clean_archive.writestr(name, data)
                return output.getvalue()
    except (BadZipFile, DocxExtractionError, KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
        raise DocxExportError("The redlined document could not be flattened to a clean copy.") from exc


def _accept_revisions_element(element: ET.Element) -> None:
    """Recursively accept all tracked changes within ``element``, in place: drop
    ``<w:del>`` and ``<w:rPrChange>``/``<w:pPrChange>`` entirely, and replace each
    ``<w:ins>`` with its (already-accepted) children."""
    ins_tag = _w_tag("ins")
    del_tag = _w_tag("del")
    drop_tags = {_w_tag("rPrChange"), _w_tag("pPrChange")}
    result: List[ET.Element] = []
    for child in list(element):
        if child.tag == del_tag or child.tag in drop_tags:
            continue
        _accept_revisions_element(child)
        if child.tag == ins_tag:
            result.extend(list(child))
        else:
            result.append(child)
    for child in list(element):
        element.remove(child)
    for child in result:
        element.append(child)


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
        primary_edit = next((edit for edit in edits if not redline_edit_contract.is_insertion_redline_edit(edit)), None)

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

        for insertion in [edit for edit in edits if redline_edit_contract.is_insertion_redline_edit(edit)]:
            for insert_paragraph in _tracked_insert_paragraphs(redline_edit_contract.redline_inserted_text(insertion), revision_id):
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

    if redline_edit_contract.is_insertion_redline_edit(redline):
        output.append(_label_value("Anchor paragraph", redline.get("anchor_text")))
        output.append(_label_value("Insert text", redline_edit_contract.redline_inserted_text(redline)))
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

    for redline in redline_edit_contract.normalize_redline_edits(redlines):
        if not isinstance(redline, dict):
            continue
        clause_id = str(redline.get("clause_id", ""))
        grouped.setdefault(clause_id, []).append(redline)
    return grouped


def _redlines_by_paragraph(redlines: object) -> Dict[str, List[RedlineEdit]]:
    grouped: Dict[str, List[RedlineEdit]] = {}
    if not isinstance(redlines, list):
        return grouped

    for redline in redline_edit_contract.normalize_redline_edits(redlines):
        if not isinstance(redline, dict):
            continue
        paragraph_id = str(redline.get("paragraph_id", ""))
        grouped.setdefault(paragraph_id, []).append(redline)
    return grouped


def _redlines_by_source_paragraph(
    redlines: object,
    source_paragraphs: List[SourceParagraph],
    review_paragraphs: object = None,
) -> Tuple[Dict[int, List[RedlineEdit]], List[RedlineEdit], List[RedlineEdit]]:
    grouped: Dict[int, List[RedlineEdit]] = {}
    unresolved: List[RedlineEdit] = []
    pdf_uncertain: List[RedlineEdit] = []
    if not isinstance(redlines, list):
        return grouped, unresolved, pdf_uncertain

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
        redline_edit_contract.normalize_redline_edits(redlines, require_content=True),
        key=redline_edit_contract.redline_resolution_order,
    ):
        review_key = redline_edit_contract.redline_review_paragraph_key(redline)
        source_paragraph = resolved_by_review_key.get(review_key) if review_key is not None else None
        if source_paragraph is None:
            source_paragraph = _resolve_source_paragraph(
                redline, source_paragraphs, review_paragraphs_by_id, claimed_indexes=claimed_indexes
            )
            if source_paragraph is None:
                # No fresh physical <w:p> claimable. Before rejecting, check the
                # split-block case: this redline's block lives *inside* an already
                # claimed physical paragraph (two review paragraphs split one <w:p>
                # on an internal blank line, sharing its source_index). Those must
                # SHARE that one physical paragraph -- applied as block-aware
                # sub-span edits later -- not be dropped or hard-failed.
                source_paragraph = _resolve_shared_split_block_paragraph(
                    redline, source_paragraphs, review_paragraphs_by_id, claimed_indexes
                )
            if source_paragraph is None:
                # A PDF-source redline that could not be confidently text-anchored
                # must NEVER be silently dropped (the original P0 defect): collect it
                # so the caller can fail closed (strict) or flag the export as an
                # incomplete redline (lenient). Genuine supplemental parts
                # (header/footer) target regions outside the body paragraph sequence
                # and remain a logged skip -- they are not PDF body content.
                if redline_edit_contract.is_pdf_source_redline(redline, review_paragraphs_by_id):
                    pdf_uncertain.append(redline)
                    continue
                if not redline_edit_contract.is_supplemental_part_redline(
                    redline, review_paragraphs_by_id
                ):
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
    return grouped, unresolved, pdf_uncertain


def _resolve_shared_split_block_paragraph(
    redline: RedlineEdit,
    source_paragraphs: List[SourceParagraph],
    review_paragraphs_by_id: Dict[str, Paragraph],
    claimed_indexes: set[int],
) -> SourceParagraph | None:
    """Resolve a redline whose review paragraph is one block of a physical <w:p>
    that already holds another redlined block (the split-on-blank-line case).

    The physical paragraph's text is the blocks joined by blank lines, so it never
    equals (and rarely text-anchors to) a single block. We match by the redline's
    provenance source_index and confirm the block is genuinely a sub-block of that
    physical paragraph before sharing it; otherwise we decline so a real mismatch
    still surfaces as unresolved rather than corrupting an unrelated paragraph."""
    source_index = _redline_source_index(redline)
    if source_index is None or source_index not in claimed_indexes:
        return None
    candidate = next(
        (paragraph for paragraph in source_paragraphs if paragraph.source_index == source_index),
        None,
    )
    if candidate is None:
        return None
    block_text = _redline_block_text(redline, review_paragraphs_by_id)
    if not block_text:
        return None
    physical_blocks = _physical_paragraph_blocks(candidate.text)
    if len(physical_blocks) < 2:
        return None
    normalized_block = _normalize_paragraph_text(block_text)
    if any(_normalize_paragraph_text(block) == normalized_block for block in physical_blocks):
        return candidate
    return None


def _redline_block_text(redline: RedlineEdit, review_paragraphs_by_id: Dict[str, Paragraph]) -> str:
    """The verbatim source text of the single review-paragraph block a redline
    targets (its ``original_text``, falling back to the review paragraph's text)."""
    original = str(redline.get("original_text") or "").strip()
    if original:
        return original
    review_paragraph = review_paragraphs_by_id.get(str(redline.get("paragraph_id") or ""))
    if isinstance(review_paragraph, dict):
        return str(review_paragraph.get("text") or "").strip()
    return ""


def _physical_paragraph_blocks(text: str) -> List[str]:
    """Split a physical paragraph's text into the same logical blocks the review
    pipeline derives from it, so a redline's block can be located within it."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", str(text or ""))]
    return [block for block in blocks if block]


def _redline_review_paragraph_key(redline: RedlineEdit) -> Tuple | None:
    return redline_edit_contract.redline_review_paragraph_key(redline)


def _redline_resolution_order(redline: RedlineEdit) -> Tuple[int, int]:
    return redline_edit_contract.redline_resolution_order(redline)


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
    claimed_indexes = claimed_indexes if claimed_indexes is not None else set()
    if redline_edit_contract.is_pdf_source_redline(redline, review_paragraphs_by_id):
        # PDF redlines anchor by CONFIDENT TEXT MATCH only. The reconstructed body
        # text is engine-independent and reliable; the loose PDF paragraph index is
        # not, so we never fall back to a positional source_index here. Place only
        # when exactly one still-unclaimed body paragraph matches the redline's text
        # within the high threshold -- ambiguous or no match declines (the caller
        # then routes it to the fail-closed / incomplete-label path, never a silent
        # drop).
        return _resolve_pdf_source_paragraph(
            redline, source_paragraphs, review_paragraphs_by_id, claimed_indexes
        )
    if _redline_source_part(redline, review_paragraphs_by_id):
        # A genuine supplemental part (header/footer): not in the body paragraph
        # sequence, so it cannot anchor here. Declined (and skipped with a warning by
        # the caller) -- unchanged behavior.
        return None
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


def _resolve_pdf_source_paragraph(
    redline: RedlineEdit,
    source_paragraphs: List[SourceParagraph],
    review_paragraphs_by_id: Dict[str, Paragraph],
    claimed_indexes: set[int],
) -> SourceParagraph | None:
    """Anchor a PDF-source redline into the reconstructed body by CONFIDENT TEXT
    MATCH, declining anything ambiguous.

    The redline's ``original_text`` (then the review paragraph's text) is compared
    against each still-unclaimed body paragraph via the contract's confident match
    (normalized equality or token-set ratio >= the PDF threshold). Returns the
    paragraph only when exactly ONE body paragraph matches a given anchor text;
    zero or multiple matches decline so the caller fails closed / labels the export
    incomplete instead of guessing. The positional PDF index is deliberately never
    consulted -- it is engine-dependent and unreliable.
    """
    for anchor_text in _redline_anchor_texts(redline, review_paragraphs_by_id):
        matches = [
            paragraph
            for paragraph in source_paragraphs
            if paragraph.source_index not in claimed_indexes
            and redline_edit_contract.confident_text_match(paragraph.text, anchor_text)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Several body paragraphs match this anchor confidently: ambiguous, so
            # the redline cannot be placed with confidence. Decline rather than pick
            # arbitrarily (which could land an accepted change on the wrong clause).
            return None
    return None


def _redline_source_index(redline: RedlineEdit) -> int | None:
    return redline_edit_contract.redline_source_index(redline)


def _redline_source_part(redline: RedlineEdit, review_paragraphs_by_id: Dict[str, Paragraph]) -> str:
    return redline_edit_contract.redline_source_part(redline, review_paragraphs_by_id)


def _redline_anchor_texts(redline: RedlineEdit, review_paragraphs_by_id: Dict[str, Paragraph]) -> List[str]:
    return redline_edit_contract.redline_anchor_texts(redline, review_paragraphs_by_id)


def _apply_redline_edits_to_source_document(
    document_root: ET.Element,
    redlines: object,
    review_paragraphs: object = None,
    *,
    strict: bool = True,
) -> Tuple[Dict[int, ET.Element], List[RedlineEdit]]:
    """Apply redlines to the body and report PDF redlines that could not anchor.

    ``strict`` (fail-closed, the default) raises ``PdfRedlineAnchorError`` when ANY
    PDF-source redline could not be confidently placed, so send/approve/export never
    emit a Word file missing accepted changes. ``strict=False`` (preview / draft /
    diagnostic) instead returns those unplaceable redlines so the caller can produce
    the file but mark it an incomplete redline. Either way a PDF redline is never
    silently dropped. Returns ``(source_paragraph_by_index, pdf_uncertain_redlines)``.
    """
    source_paragraphs = _indexed_source_paragraphs(document_root)
    source_paragraph_by_index = {
        paragraph.source_index: paragraph.paragraph
        for paragraph in source_paragraphs
    }
    redlines_by_source_index, unresolved_redlines, pdf_uncertain_redlines = _redlines_by_source_paragraph(
        redlines,
        source_paragraphs,
        review_paragraphs,
    )
    if unresolved_redlines:
        raise DocxExportError(_unanchored_redline_error(unresolved_redlines))
    if pdf_uncertain_redlines and strict:
        # Fail closed: rather than ship a reconstructed Word doc that silently omits
        # these accepted changes, abort so the caller surfaces the recovery path.
        raise PdfRedlineAnchorError(pdf_uncertain_redlines)
    if not redlines_by_source_index:
        return source_paragraph_by_index, pdf_uncertain_redlines

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

        primary_edits = [edit for edit in edits if not redline_edit_contract.is_insertion_redline_edit(edit)]
        insert_position = paragraph_position + 1
        physical_blocks = _physical_paragraph_blocks(source_paragraph.text)
        block_paragraphs: List[ET.Element] = []
        if len(physical_blocks) > 1 and primary_edits:
            # Split-block physical paragraph: several review paragraphs share this
            # one <w:p>. Rebuilding it from a single edit's text would clobber the
            # sibling blocks (silent data loss). Re-emit it one tracked paragraph
            # per block so every block's content survives.
            block_paragraphs, revision_id = _combined_block_aware_redline_paragraphs(
                source_paragraph,
                physical_blocks,
                primary_edits,
                revision_id,
            )
        if block_paragraphs:
            source_paragraph.parent[paragraph_position] = block_paragraphs[0]
            source_paragraph_by_index[source_paragraph.source_index] = block_paragraphs[0]
            for offset, block_paragraph in enumerate(block_paragraphs[1:], start=1):
                source_paragraph.parent.insert(paragraph_position + offset, block_paragraph)
            insert_position = paragraph_position + len(block_paragraphs)
        else:
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
                    source_paragraph_by_index[source_paragraph.source_index] = primary_paragraph
                    primary_applied = True

        for insertion in [edit for edit in edits if redline_edit_contract.is_insertion_redline_edit(edit)]:
            insert_text = redline_edit_contract.redline_inserted_text(insertion)
            for inserted_paragraph in _source_tracked_insert_paragraphs(insert_text, revision_id):
                source_paragraph.parent.insert(insert_position, inserted_paragraph)
                insert_position += 1
                revision_id += 1
    return source_paragraph_by_index, pdf_uncertain_redlines


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
    action = redline.get("action")
    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH} and _paragraph_has_nontext_inline_content(
        source_paragraph
    ):
        raise DocxExportError(
            "The uploaded Word document cannot safely redline an edited paragraph that contains "
            "inline objects, hyperlinks, fields, or note references without dropping source content."
        )
    if action == REDLINE_REPLACE_PARAGRAPH:
        # When the edited paragraph's run model is attached, re-emit the inserted text
        # as FORMATTED runs (bold/italic/font/size) so the clean export keeps the
        # paragraph's formatting. ADDITIVE and gated on replacement_runs being a
        # non-empty list -- redlines without it keep the existing char/word diff path.
        replacement_runs = redline.get("replacement_runs")
        if isinstance(replacement_runs, list) and replacement_runs:
            return _source_tracked_replace_paragraph_runs(
                source_paragraph,
                original_text,
                replacement_runs,
                revision_id,
            )
        # A free-form manual viewer edit diffs at the CHARACTER level (mirroring the
        # frontend redline preview), so only the changed letters redline. Clause and
        # governing-law replacements stay whole-paragraph; they (and any manual edit
        # explicitly flagged whole_paragraph) keep the token-level path.
        is_freeform_manual_edit = redline_edit_contract.is_freeform_manual_replace_edit(redline)
        replace_paragraph = (
            _source_tracked_replace_paragraph_char
            if is_freeform_manual_edit
            else _source_tracked_replace_paragraph
        )
        return replace_paragraph(
            source_paragraph,
            original_text,
            str(redline.get("replacement_text") or ""),
            revision_id,
        )
    if action == REDLINE_DELETE_PARAGRAPH:
        return _source_tracked_delete_paragraph(source_paragraph, original_text, revision_id), revision_id + 1
    if action == REDLINE_FORMAT_PARAGRAPH:
        format_ops = list(redline.get("format_ops")) if isinstance(redline.get("format_ops"), list) else []
        paragraph_ops = [op for op in format_ops if isinstance(op, dict) and op.get("scope") == "paragraph"]
        run_ops = [op for op in format_ops if isinstance(op, dict) and op.get("scope") == "run"]
        # Paragraph ops first (they rebuild the <w:p> and emit the <w:pPrChange>),
        # then run ops applied to that result (each emits a per-range <w:rPrChange>).
        formatted, revision_id = _apply_tracked_paragraph_format(
            source_paragraph,
            paragraph_ops,
            revision_id,
        )
        formatted, revision_id = _apply_tracked_run_format(formatted, run_ops, revision_id)
        return formatted, revision_id
    return None, revision_id


def _paragraph_has_nontext_inline_content(paragraph: ET.Element) -> bool:
    """Return True when a replace/delete would flatten non-text inline content.

    Whole-paragraph replace/delete paths rebuild the paragraph from plain tracked
    text. If the source paragraph carries drawings/pictures, hyperlinks, fields,
    or note references, rebuilding would silently discard those structures. Fail
    safe until a run-level object-preserving edit path exists.
    """
    risky_tags = {
        _w_tag("drawing"),
        _w_tag("pict"),
        _w_tag("object"),
        _w_tag("hyperlink"),
        _w_tag("fldSimple"),
        _w_tag("instrText"),
        _w_tag("fldChar"),
        _w_tag("footnoteReference"),
        _w_tag("endnoteReference"),
    }
    return any(node.tag in risky_tags for node in paragraph.iter())


def _combined_block_aware_redline_paragraphs(
    source_paragraph: SourceParagraph,
    physical_blocks: List[str],
    primary_edits: List[RedlineEdit],
    revision_id: int,
) -> Tuple[List[ET.Element], int]:
    """Redline a split-block physical <w:p> as one tracked paragraph PER block, so
    no sibling block is destroyed.

    Several review paragraphs split this one physical <w:p> on an internal blank
    line. Rebuilding the whole <w:p> from a single edit's text would clobber the
    other blocks (silent data loss). Instead each block becomes its own tracked
    paragraph -- replaced where a redline targets it, deleted where a delete targets
    it, verbatim (with the source paragraph's properties) otherwise. This mirrors
    the review model's one-paragraph-per-block view and keeps every block's content.
    Returns an empty list when no edit matched a block, so the caller leaves the
    paragraph untouched rather than restructuring it needlessly."""
    edits_by_block: Dict[str, RedlineEdit] = {}
    for edit in primary_edits:
        normalized = _normalize_paragraph_text(str(edit.get("original_text") or ""))
        if normalized and normalized not in edits_by_block:
            edits_by_block[normalized] = edit

    matched_any = False
    rendered: List[ET.Element] = []
    for block in physical_blocks:
        edit = edits_by_block.get(_normalize_paragraph_text(block))
        if edit is None:
            # Unedited block: keep it verbatim, carrying the source properties.
            rendered.append(_source_verbatim_paragraph(source_paragraph.paragraph, block))
            continue
        # Re-base the edit to THIS block, not the whole physical <w:p>. The redline's
        # offsets (run-format ops in particular) are relative to the single block's
        # text -- passing the whole physical paragraph would land run ops on the wrong
        # block's characters. Build a single-block source <w:p> (the block text
        # inheriting the source paragraph's properties, mirroring the verbatim path),
        # so run-format offsets index from block-local 0.
        block_source_paragraph = _source_verbatim_paragraph(source_paragraph.paragraph, block)
        block_paragraph, revision_id = _source_tracked_primary_redline_paragraph(
            block_source_paragraph,
            {**edit, "original_text": block},
            revision_id,
        )
        if block_paragraph is None:
            rendered.append(_source_verbatim_paragraph(source_paragraph.paragraph, block))
            continue
        matched_any = True
        rendered.append(block_paragraph)

    if not matched_any:
        return [], revision_id
    return rendered, revision_id


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


def _targeted_source_comments(
    review_result: ReviewResult,
    source_paragraph_by_index: Dict[int, ET.Element],
) -> List[dict]:
    comments = _prepared_review_comments(review_result)
    review_paragraphs = _review_paragraphs_by_id(review_result.get("paragraphs", []))
    targeted: List[dict] = []
    for comment in comments:
        source_index = _comment_source_index(comment, review_paragraphs)
        if source_index is None or source_index not in source_paragraph_by_index:
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
            "parent_id": str(comment.get("parent_id") or "").strip(),
            "resolved": bool(comment.get("resolved")),
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
        # Replies (non-empty parent_id) are comment entries with NO in-document
        # range; only the thread root gets the commentRangeStart/End + reference run.
        if str(comment.get("parent_id") or "").strip():
            continue
        paragraph = report_paragraph_by_id.get(str(comment.get("_report_paragraph_id") or ""))
        if paragraph is not None:
            _apply_comment_anchor(paragraph, comment)


def _apply_comment_anchors_to_source_document(
    source_paragraph_by_index: Dict[int, ET.Element],
    comments: List[dict],
) -> None:
    for comment in comments:
        # Replies (non-empty parent_id) are comment entries with NO in-document
        # range; only the thread root gets the commentRangeStart/End + reference run.
        if str(comment.get("parent_id") or "").strip():
            continue
        paragraph = source_paragraph_by_index.get(comment.get("_source_index"))
        if paragraph is not None:
            _apply_comment_anchor(paragraph, comment)


def _targetable_source_paragraphs_by_index(document_root: ET.Element) -> Dict[int, ET.Element]:
    return {
        paragraph.source_index: paragraph.paragraph
        for paragraph in _indexed_source_paragraphs(document_root)
    }


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
        f'  <Override PartName="/word/commentsExtended.xml" ContentType="{COMMENTS_EXTENDED_CONTENT_TYPE}"/>\n'
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
        f'  <Relationship Id="rId4" Type="{COMMENTS_EXTENDED_RELATIONSHIP_TYPE}" Target="commentsExtended.xml"/>\n'
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
        _ensure_relationship_target(
            relationships_root, COMMENTS_EXTENDED_RELATIONSHIP_TYPE, "commentsExtended.xml"
        )
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
        _ensure_content_type_override(
            content_types_root, "/word/commentsExtended.xml", COMMENTS_EXTENDED_CONTENT_TYPE
        )
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
