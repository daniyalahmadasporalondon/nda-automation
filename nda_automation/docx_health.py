from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Dict, List, Tuple
from zipfile import BadZipFile, ZipFile

from .docx_xml import UnsafeDocxXmlError, parse_docx_xml
from .docx_text import DocxExtractionError, validate_docx_archive

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
        with ZipFile(BytesIO(docx_bytes)) as archive:
            try:
                validate_docx_archive(archive)
            except DocxExtractionError as exc:
                errors.append(str(exc))
                return errors

            corrupt_part = archive.testzip()
            if corrupt_part:
                errors.append(f"ZIP integrity check failed at {corrupt_part}.")
            names = set(archive.namelist())
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
    except BadZipFile:
        errors.append("Export is not a readable DOCX zip package.")
    return errors


def exported_document_text(docx_bytes: bytes) -> str:
    """Visible + tracked-deleted text of the export (w:t and w:delText nodes)."""
    try:
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


def verify_export_content_coverage(docx_bytes: bytes, source_text: str) -> List[str]:
    """Content gate the structural health check misses: an empty body or a
    truncated redline that drops source content. Returns error strings (counts
    only, never source text, to avoid leaking NDA content)."""
    source_normalized = _normalize_export_text(source_text)
    if not source_normalized:
        return []
    export_normalized = _normalize_export_text(exported_document_text(docx_bytes))
    if not export_normalized:
        return ["Exported document body contains no text."]
    if len(export_normalized) < len(source_normalized) * EXPORT_CONTENT_COVERAGE_RATIO:
        return [
            f"Exported text covers only {len(export_normalized)} of {len(source_normalized)} "
            "source characters; the redline may have dropped source content."
        ]
    return []


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
