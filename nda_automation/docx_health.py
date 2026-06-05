from __future__ import annotations

from collections import Counter
import posixpath
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Dict, List, Tuple
from zipfile import BadZipFile, ZipFile

from .docx_xml import UnsafeDocxXmlError, parse_docx_xml
from .docx_text import DocxExtractionError, validate_docx_archive, validate_docx_bytes_before_open
from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)

# Tracked redlines only add text (insertions as w:t, deletions retained as
# w:delText), so the exported visible text is always >= the source text. An
# export that covers far less than the source has dropped/empty content.
EXPORT_CONTENT_COVERAGE_RATIO = 0.5

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
SETTINGS_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
OFFICE_DOCUMENT_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
STYLE_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
DOCUMENT_CONTENT_TYPE = f"{DOCX_MIME}.main+xml"
RELATIONSHIPS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
SETTINGS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
STYLES_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"


def validate_docx_open_health(docx_bytes: bytes, require_styles: bool = False) -> List[str]:
    errors: List[str] = []
    required_parts = {
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
        "word/settings.xml",
    }
    if require_styles:
        required_parts.add("word/styles.xml")

    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            try:
                validate_docx_archive(archive)
            except DocxExtractionError as exc:
                errors.append(str(exc))
                return errors

            corrupt_part = archive.testzip()
            if corrupt_part:
                errors.append(f"ZIP integrity check failed at {corrupt_part}.")
            archive_names = archive.namelist()
            duplicate_names = sorted(name for name, count in Counter(archive_names).items() if count > 1)
            if duplicate_names:
                errors.append(f"DOCX package contains duplicate entries: {', '.join(duplicate_names)}.")
            names = set(archive_names)
            missing_parts = sorted(required_parts - names)
            if missing_parts:
                errors.append(f"Missing DOCX parts: {', '.join(missing_parts)}.")
                return errors

            try:
                defaults, overrides = _docx_content_types(archive)
            except (KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
                errors.append(f"Content types are unreadable: {exc}.")
                return errors

            if defaults.get("rels") != RELATIONSHIPS_CONTENT_TYPE:
                errors.append("Missing or invalid .rels content type default.")
            if defaults.get("xml") != "application/xml":
                errors.append("Missing or invalid .xml content type default.")
            if overrides.get("/word/document.xml") != DOCUMENT_CONTENT_TYPE:
                errors.append("Missing or invalid document.xml content type override.")
            if overrides.get("/word/settings.xml") != SETTINGS_CONTENT_TYPE:
                errors.append("Missing or invalid settings.xml content type override.")
            if "word/styles.xml" in names and overrides.get("/word/styles.xml") != STYLES_CONTENT_TYPE:
                errors.append("Missing or invalid styles.xml content type override.")

            try:
                package_relationships = _relationship_targets(archive, "_rels/.rels")
                document_relationships = _relationship_targets(archive, "word/_rels/document.xml.rels")
            except (KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
                errors.append(f"Relationships are unreadable: {exc}.")
                return errors

            office_document_targets = [
                _resolve_relationship_target("_rels/.rels", relationship["Target"])
                for relationship in package_relationships
                if relationship.get("Type") == OFFICE_DOCUMENT_RELATIONSHIP_TYPE and "Target" in relationship
            ]
            if office_document_targets != ["word/document.xml"]:
                errors.append("Package relationships do not resolve to word/document.xml.")

            document_targets_by_type = {
                relationship["Type"]: _resolve_relationship_target("word/_rels/document.xml.rels", relationship["Target"])
                for relationship in document_relationships
                if relationship.get("TargetMode") != "External" and "Target" in relationship and "Type" in relationship
            }
            for relationship_type, target in document_targets_by_type.items():
                if target not in names:
                    errors.append(f"Relationship target is missing: {relationship_type} -> {target}.")
            if document_targets_by_type.get(SETTINGS_RELATIONSHIP_TYPE) != "word/settings.xml":
                errors.append("Document relationships do not resolve settings.xml.")
            if require_styles and document_targets_by_type.get(STYLE_RELATIONSHIP_TYPE) != "word/styles.xml":
                errors.append("Document relationships do not resolve styles.xml.")
            if not require_styles and STYLE_RELATIONSHIP_TYPE in document_targets_by_type and document_targets_by_type[STYLE_RELATIONSHIP_TYPE] != "word/styles.xml":
                errors.append("Document styles relationship does not resolve styles.xml.")

            try:
                settings_root = parse_docx_xml(archive.read("word/settings.xml"), part_name="word/settings.xml")
            except (KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
                errors.append(f"settings.xml is unreadable: {exc}.")
                return errors
            if settings_root.find(_w_tag("trackRevisions")) is None:
                errors.append("settings.xml does not enable Track Changes.")

            try:
                document_root = parse_docx_xml(archive.read("word/document.xml"), part_name="word/document.xml")
            except (KeyError, ET.ParseError, UnsafeDocxXmlError) as exc:
                errors.append(f"document.xml is unreadable: {exc}.")
                return errors
            body = document_root.find(_w_tag("body"))
            if body is None:
                errors.append("document.xml is missing w:body.")
            elif body.find(_w_tag("sectPr")) is None:
                errors.append("document.xml is missing section properties.")
            if document_root.findall(f".//{_w_tag('pPr')}/{_w_tag('rPr')}/{_w_tag('ins')}"):
                errors.append("document.xml contains insertion revision markup inside paragraph properties.")
            if document_root.findall(f".//{_w_tag('pPr')}/{_w_tag('rPr')}/{_w_tag('del')}"):
                errors.append("document.xml contains deletion revision markup inside paragraph properties.")
    except DocxExtractionError as exc:
        errors.append(str(exc))
    except BadZipFile:
        errors.append("Export is not a readable DOCX zip package.")
    return errors


def exported_document_text(docx_bytes: bytes) -> str:
    """Visible + tracked-deleted text of the export (w:t and w:delText nodes)."""
    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            validate_docx_archive(archive)
            document_root = parse_docx_xml(archive.read("word/document.xml"), part_name="word/document.xml")
    except (BadZipFile, DocxExtractionError, KeyError, ET.ParseError, UnsafeDocxXmlError):
        return ""
    parts = [
        node.text or ""
        for node in document_root.iter()
        if node.tag in (_w_tag("t"), _w_tag("delText"))
    ]
    return " ".join(parts)


def verify_export_content_coverage(
    docx_bytes: bytes,
    source_text: str,
    *,
    expected_redline_edits: object = None,
) -> List[str]:
    """Content gate the structural health check misses: an empty body or a
    redline that drops, reorders, duplicates, or misplaces source content.
    Returns error strings (counts only, never source text, to avoid leaking NDA content)."""
    source_normalized = _normalize_export_text(source_text)
    if not source_normalized:
        return []
    export_paragraphs = _export_revision_paragraphs(docx_bytes)
    export_normalized = _normalize_export_text(" ".join(record["all"] for record in export_paragraphs))
    if not export_normalized:
        return ["Exported document body contains no text."]
    if len(export_normalized) < len(source_normalized) * EXPORT_CONTENT_COVERAGE_RATIO:
        return [
            f"Exported text covers only {len(export_normalized)} of {len(source_normalized)} "
            "source characters; the redline may have dropped source content."
        ]
    source_paragraphs = _source_paragraphs_from_text(source_text)
    if source_paragraphs:
        expected_accepted_paragraphs, expected_errors = _expected_accepted_source_paragraphs(
            source_paragraphs,
            expected_redline_edits,
        )
        if expected_errors:
            return expected_errors
        accepted_paragraphs = [record["accepted"] for record in export_paragraphs if record["accepted"]]
        if accepted_paragraphs != expected_accepted_paragraphs:
            return [
                "Exported accepted-change paragraph sequence does not match the expected source/redline "
                f"sequence ({len(accepted_paragraphs)} paragraph(s); expected {len(expected_accepted_paragraphs)}). "
                "The redline may have misplaced, duplicated, or dropped source content."
            ]
    return []


def _export_revision_paragraphs(docx_bytes: bytes) -> List[Dict[str, str]]:
    try:
        validate_docx_bytes_before_open(docx_bytes)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            validate_docx_archive(archive)
            document_root = parse_docx_xml(archive.read("word/document.xml"), part_name="word/document.xml")
    except (BadZipFile, DocxExtractionError, KeyError, ET.ParseError, UnsafeDocxXmlError):
        return []

    return [
        {
            "accepted": _normalize_export_text(_paragraph_revision_text(paragraph, accepted=True)),
            "all": _normalize_export_text(_paragraph_all_revision_text(paragraph)),
            "rejected": _normalize_export_text(_paragraph_revision_text(paragraph, accepted=False)),
        }
        for paragraph in document_root.iter(_w_tag("p"))
    ]


def _paragraph_revision_text(node: ET.Element, *, accepted: bool) -> str:
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "del":
        return "" if accepted else "".join(_paragraph_revision_text(child, accepted=False) for child in list(node))
    if tag == "ins":
        return "".join(_paragraph_revision_text(child, accepted=True) for child in list(node)) if accepted else ""
    if tag in {"t", "delText"}:
        return node.text or ""
    if tag == "br":
        return "\n"
    return "".join(_paragraph_revision_text(child, accepted=accepted) for child in list(node))


def _paragraph_all_revision_text(paragraph: ET.Element) -> str:
    return "".join(
        node.text or ""
        for node in paragraph.iter()
        if node.tag in (_w_tag("t"), _w_tag("delText"))
    )


def _source_paragraphs_from_text(source_text: str) -> List[str]:
    return [
        normalized
        for paragraph in re.split(r"\n\s*\n+", str(source_text or ""))
        if (normalized := _normalize_export_text(paragraph))
    ]


def _expected_accepted_source_paragraphs(
    source_paragraphs: List[str],
    expected_redline_edits: object,
) -> Tuple[List[str], List[str]]:
    expected = list(source_paragraphs)
    errors: List[str] = []
    expected_insertions_by_source_index: Dict[int, List[str]] = {}
    if not isinstance(expected_redline_edits, list):
        return expected, []

    for redline in expected_redline_edits:
        if not isinstance(redline, dict):
            continue
        action = str(redline.get("action") or "")
        source_index = _expected_redline_source_index(redline)
        if source_index is None:
            continue
        if source_index < 1 or source_index > len(source_paragraphs):
            errors.append(f"Redline {_redline_label(redline)} targets missing source paragraph {source_index}.")
            continue

        if action == REDLINE_REPLACE_PARAGRAPH:
            expected[source_index - 1] = _normalize_export_text(redline.get("replacement_text"))
        elif action == REDLINE_DELETE_PARAGRAPH:
            expected[source_index - 1] = ""
        elif action == REDLINE_INSERT_AFTER_PARAGRAPH:
            expected_insertions_by_source_index.setdefault(source_index, []).extend(
                _redline_text_blocks(redline.get("insert_text") or redline.get("replacement_text") or "")
            )

    if errors:
        return [], errors

    accepted: List[str] = []
    for source_index, paragraph in enumerate(expected, start=1):
        if paragraph:
            accepted.append(paragraph)
        accepted.extend(expected_insertions_by_source_index.get(source_index, []))
    return accepted, []


def _expected_redline_source_index(redline: Dict[str, object]) -> int | None:
    # The expected sequence is built over the blank-line-split source blocks
    # (_source_paragraphs_from_text), whose 1-based ordinal is the review
    # paragraph_index. Prefer paragraph_index over source_index: source_index is
    # provenance and is shared by the parts of one extracted block that split on an
    # internal blank line, so keying on it would map two redlines to one block and
    # spuriously fail the content-coverage check. paragraph_index is unique per
    # block. source_index remains the fallback for redlines that carry no index.
    for key in ("paragraph_index", "source_index"):
        value = redline.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _redline_text_blocks(value: object) -> List[str]:
    blocks = [
        normalized
        for block in str(value or "").split("\n\n")
        if (normalized := _normalize_export_text(block))
    ]
    return blocks


def _redline_label(redline: Dict[str, object]) -> str:
    for key in ("id", "clause_id", "paragraph_id"):
        value = str(redline.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _normalize_export_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _docx_content_types(archive: ZipFile) -> Tuple[Dict[str, str], Dict[str, str]]:
    content_types_root = parse_docx_xml(archive.read("[Content_Types].xml"), part_name="[Content_Types].xml")
    defaults = {
        default.attrib["Extension"]: default.attrib["ContentType"]
        for default in content_types_root.findall(_content_type_tag("Default"))
        if "Extension" in default.attrib and "ContentType" in default.attrib
    }
    overrides = {
        override.attrib["PartName"]: override.attrib["ContentType"]
        for override in content_types_root.findall(_content_type_tag("Override"))
        if "PartName" in override.attrib and "ContentType" in override.attrib
    }
    return defaults, overrides


def _relationship_targets(archive: ZipFile, relationship_part: str) -> List[Dict[str, str]]:
    relationships_root = parse_docx_xml(archive.read(relationship_part), part_name=relationship_part)
    return [
        dict(relationship.attrib)
        for relationship in relationships_root.findall(_rel_tag("Relationship"))
    ]


def _resolve_relationship_target(relationship_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.removeprefix("/")
    if relationship_part == "_rels/.rels":
        base_dir = ""
    else:
        rels_dir = posixpath.dirname(relationship_part)
        base_dir = posixpath.dirname(rels_dir)
    return posixpath.normpath(posixpath.join(base_dir, target))


def _w_tag(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _rel_tag(tag: str) -> str:
    return f"{{{REL_NS}}}{tag}"


def _content_type_tag(tag: str) -> str:
    return f"{{{CONTENT_TYPES_NS}}}{tag}"
