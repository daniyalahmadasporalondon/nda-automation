from __future__ import annotations

from io import BytesIO
import re
from typing import Any, Dict, Iterable, List
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from .docx_xml import UnsafeDocxXmlError, is_docx_xml_part, parse_docx_xml, reject_unsafe_docx_xml

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_DOCX_ENTRY_COMPRESSION_RATIO = 100
MAX_DOCX_ZIP_ENTRIES = 4096
MAX_DOCX_TABLE_NESTING_DEPTH = 64
ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
ZIP_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
ZIP_EOCD_MIN_SIZE = 22
ZIP_MAX_COMMENT_BYTES = 65_535
ZIP64_16BIT_SENTINEL = 0xFFFF
ZIP64_32BIT_SENTINEL = 0xFFFFFFFF
DOCX_TOO_LARGE_MESSAGE = "The Word document is too large after decompression."
DOCX_SUSPICIOUS_COMPRESSION_MESSAGE = "The Word document uses a suspicious compression ratio."
DOCX_TOO_MANY_ENTRIES_MESSAGE = "The Word document contains too many archive entries."
DOCX_UNSUPPORTED_ZIP64_MESSAGE = "The Word document uses unsupported ZIP64 archive metadata."
DOCX_TABLE_NESTING_MESSAGE = "The Word document contains tables nested too deeply."
DOCX_XML_NESTING_MESSAGE = "The Word document XML is too deeply nested."
# Supplemental parts whose text is part of the agreement and must be reviewed
# (headers, footers, footnotes, endnotes). word/comments.xml is deliberately
# excluded: it holds counterparty/reviewer annotations, not body text, and
# feeding it to the verdict engine lets a comment like "check non-circumvention"
# manufacture a clause hit that the agreement itself never makes. Comments are
# still surfaced separately on the export path (see docx_comments.py); they just
# never reach the clause checkers.
SUPPLEMENTAL_PART_PREFIXES = (
    "word/header",
    "word/footer",
    "word/footnotes.xml",
    "word/endnotes.xml",
)
DocxParagraph = Dict[str, object]
NumberingDefinitions = Dict[str, object]
StyleDefinitions = Dict[str, Dict[str, object]]


class DocxExtractionError(ValueError):
    """Raised when a DOCX file cannot be converted into reviewable text."""


def extract_docx_text(data: bytes) -> str:
    paragraphs = extract_docx_paragraphs(data)
    return "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs).strip()


def extract_docx_paragraphs(data: bytes) -> List[DocxParagraph]:
    try:
        validate_docx_bytes_before_open(data)
        with ZipFile(BytesIO(data)) as document:
            validate_docx_archive(document)
            paragraphs = _extract_main_document_paragraphs(document)
            paragraphs.extend(_extract_supplemental_paragraphs(document))
    except BadZipFile as exc:
        raise DocxExtractionError("The uploaded file is not a valid .docx document.") from exc
    except RecursionError as exc:
        raise DocxExtractionError(DOCX_XML_NESTING_MESSAGE) from exc

    if not paragraphs:
        raise DocxExtractionError("No readable text was found in the Word document.")
    return paragraphs


def validate_docx_bytes_before_open(docx_bytes: bytes) -> None:
    """Reject hostile DOCX archives before ZipFile builds in-memory metadata."""
    entries = _scan_zip_central_directory(docx_bytes)
    _validate_docx_entry_metadata(entries)


def validate_docx_archive(document: ZipFile) -> None:
    entries = document.infolist()
    _validate_docx_entry_metadata(entries)
    for item in entries:
        if item.is_dir():
            continue
        if is_docx_xml_part(item.filename):
            try:
                reject_unsafe_docx_xml(document.read(item.filename), part_name=item.filename)
            except UnsafeDocxXmlError as exc:
                raise DocxExtractionError(str(exc)) from exc


def _scan_zip_central_directory(docx_bytes: bytes) -> List[Dict[str, object]]:
    eocd_index = _find_zip_eocd(docx_bytes)
    if eocd_index < 0:
        raise BadZipFile("File is not a zip file")

    disk_number = _zip_uint16(docx_bytes, eocd_index + 4)
    central_directory_disk = _zip_uint16(docx_bytes, eocd_index + 6)
    disk_entries = _zip_uint16(docx_bytes, eocd_index + 8)
    total_entries = _zip_uint16(docx_bytes, eocd_index + 10)
    central_directory_size = _zip_uint32(docx_bytes, eocd_index + 12)
    central_directory_offset = _zip_uint32(docx_bytes, eocd_index + 16)

    if disk_number or central_directory_disk or disk_entries != total_entries:
        raise BadZipFile("Multi-disk zip files are not supported")
    if (
        total_entries == ZIP64_16BIT_SENTINEL
        or central_directory_size == ZIP64_32BIT_SENTINEL
        or central_directory_offset == ZIP64_32BIT_SENTINEL
    ):
        raise DocxExtractionError(DOCX_UNSUPPORTED_ZIP64_MESSAGE)
    if total_entries > MAX_DOCX_ZIP_ENTRIES:
        raise DocxExtractionError(DOCX_TOO_MANY_ENTRIES_MESSAGE)

    central_directory_end = central_directory_offset + central_directory_size
    if central_directory_offset < 0 or central_directory_end > eocd_index:
        raise BadZipFile("Central directory is invalid")

    entries: List[Dict[str, object]] = []
    cursor = central_directory_offset
    for _entry_index in range(total_entries):
        if cursor + 46 > central_directory_end:
            raise BadZipFile("Central directory entry is truncated")
        if docx_bytes[cursor:cursor + 4] != ZIP_CENTRAL_DIRECTORY_SIGNATURE:
            raise BadZipFile("Central directory entry has an invalid signature")
        flags = _zip_uint16(docx_bytes, cursor + 8)
        compressed_size = _zip_uint32(docx_bytes, cursor + 20)
        uncompressed_size = _zip_uint32(docx_bytes, cursor + 24)
        filename_length = _zip_uint16(docx_bytes, cursor + 28)
        extra_length = _zip_uint16(docx_bytes, cursor + 30)
        comment_length = _zip_uint16(docx_bytes, cursor + 32)
        filename_start = cursor + 46
        filename_end = filename_start + filename_length
        next_cursor = filename_end + extra_length + comment_length
        if filename_end > central_directory_end or next_cursor > central_directory_end:
            raise BadZipFile("Central directory entry is truncated")
        if compressed_size == ZIP64_32BIT_SENTINEL or uncompressed_size == ZIP64_32BIT_SENTINEL:
            raise DocxExtractionError(DOCX_UNSUPPORTED_ZIP64_MESSAGE)
        entries.append({
            "filename": _decode_zip_filename(docx_bytes[filename_start:filename_end], flags),
            "compress_size": compressed_size,
            "file_size": uncompressed_size,
        })
        cursor = next_cursor

    if cursor != central_directory_end:
        raise BadZipFile("Central directory has trailing metadata")
    return entries


def _find_zip_eocd(docx_bytes: bytes) -> int:
    if len(docx_bytes) < ZIP_EOCD_MIN_SIZE:
        return -1
    search_start = max(0, len(docx_bytes) - ZIP_EOCD_MIN_SIZE - ZIP_MAX_COMMENT_BYTES)
    search_end = len(docx_bytes)
    while True:
        index = docx_bytes.rfind(ZIP_EOCD_SIGNATURE, search_start, search_end)
        if index < 0:
            return -1
        if index + ZIP_EOCD_MIN_SIZE <= len(docx_bytes):
            comment_length = _zip_uint16(docx_bytes, index + 20)
            if index + ZIP_EOCD_MIN_SIZE + comment_length == len(docx_bytes):
                return index
        search_end = index


def _validate_docx_entry_metadata(entries: Iterable[object]) -> None:
    entry_list = list(entries)
    if len(entry_list) > MAX_DOCX_ZIP_ENTRIES:
        raise DocxExtractionError(DOCX_TOO_MANY_ENTRIES_MESSAGE)

    total_uncompressed = 0
    for item in entry_list:
        filename = _zip_entry_filename(item)
        if _zip_entry_is_dir(item, filename):
            continue
        file_size = _zip_entry_file_size(item)
        compress_size = _zip_entry_compress_size(item)
        total_uncompressed += file_size
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise DocxExtractionError(DOCX_TOO_LARGE_MESSAGE)
        if file_size and compress_size == 0:
            raise DocxExtractionError(DOCX_SUSPICIOUS_COMPRESSION_MESSAGE)
        if compress_size and file_size / compress_size > MAX_DOCX_ENTRY_COMPRESSION_RATIO:
            raise DocxExtractionError(DOCX_SUSPICIOUS_COMPRESSION_MESSAGE)


def _zip_entry_filename(item: object) -> str:
    if isinstance(item, dict):
        return str(item.get("filename") or "")
    return str(getattr(item, "filename", ""))


def _zip_entry_file_size(item: object) -> int:
    if isinstance(item, dict):
        return int(item.get("file_size") or 0)
    return int(getattr(item, "file_size", 0) or 0)


def _zip_entry_compress_size(item: object) -> int:
    if isinstance(item, dict):
        return int(item.get("compress_size") or 0)
    return int(getattr(item, "compress_size", 0) or 0)


def _zip_entry_is_dir(item: object, filename: str) -> bool:
    is_dir = getattr(item, "is_dir", None)
    if callable(is_dir):
        return bool(is_dir())
    return filename.endswith("/")


def _zip_uint16(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise BadZipFile("Zip metadata is truncated")
    return int.from_bytes(data[offset:offset + 2], "little")


def _zip_uint32(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise BadZipFile("Zip metadata is truncated")
    return int.from_bytes(data[offset:offset + 4], "little")


def _decode_zip_filename(filename: bytes, flags: int) -> str:
    encoding = "utf-8" if flags & 0x800 else "cp437"
    return filename.decode(encoding, errors="replace")


def _extract_main_document_paragraphs(document: ZipFile) -> List[DocxParagraph]:
    root = _read_xml_part(document, "word/document.xml", missing_message="The Word document is missing its main document body.")
    styles = _read_styles(document)
    numbering = _read_numbering(document)
    numbering_state: Dict[str, Dict[int, int]] = {}
    paragraphs: List[DocxParagraph] = []
    for source_index, (paragraph, table_context) in enumerate(_iter_document_paragraphs(root), start=1):
        text = _paragraph_text(paragraph)
        if text:
            paragraphs.append(_paragraph_record(
                paragraph,
                text,
                source_index=source_index,
                styles=styles,
                numbering=numbering,
                numbering_state=numbering_state,
                table_context=table_context,
            ))
    return paragraphs


def _extract_supplemental_paragraphs(document: ZipFile) -> List[DocxParagraph]:
    paragraphs: List[DocxParagraph] = []
    for part_name in sorted(_supplemental_part_names(document)):
        root = _read_xml_part(document, part_name)
        styles = _read_styles(document)
        source_part = _source_part_label(part_name)
        for paragraph in root.iter(f"{WORD_NS}p"):
            text = _paragraph_text(paragraph)
            if text:
                paragraphs.append(_paragraph_record(
                    paragraph,
                    text,
                    source_part=source_part,
                    styles=styles,
                    numbering={},
                    numbering_state={},
                ))
    return paragraphs


def _iter_document_paragraphs(root: ET.Element) -> Iterable[tuple[ET.Element, Dict[str, int] | None]]:
    body = root.find(f"{WORD_NS}body")
    container = body if body is not None else root
    table_counter = 0

    def walk(parent: ET.Element, table_context: Dict[str, int] | None = None, table_depth: int = 0):
        nonlocal table_counter
        for child in list(parent):
            if child.tag == f"{WORD_NS}p":
                yield child, table_context
            elif child.tag == f"{WORD_NS}tbl":
                nested_table_depth = table_depth + 1
                if nested_table_depth > MAX_DOCX_TABLE_NESTING_DEPTH:
                    raise DocxExtractionError(DOCX_TABLE_NESTING_MESSAGE)
                table_counter += 1
                table_index = table_counter
                row_index = 0
                for row in _children(child, "tr"):
                    row_index += 1
                    cell_index = 0
                    for cell in _children(row, "tc"):
                        cell_index += 1
                        yield from walk(cell, {
                            "table_index": table_index,
                            "row_index": row_index,
                            "cell_index": cell_index,
                        }, nested_table_depth)
            else:
                yield from walk(child, table_context, table_depth)

    yield from walk(container)


def _paragraph_record(
    paragraph: ET.Element,
    text: str,
    *,
    styles: StyleDefinitions,
    numbering: NumberingDefinitions,
    numbering_state: Dict[str, Dict[int, int]],
    source_index: int | None = None,
    source_part: str | None = None,
    table_context: Dict[str, int] | None = None,
) -> DocxParagraph:
    record: DocxParagraph = {"text": text}
    if source_index is not None:
        record["source_index"] = source_index
    if source_part:
        record["source_part"] = source_part
    record["source_kind"] = "table_cell" if table_context else ("supplemental" if source_part else "paragraph")
    if table_context:
        record["table"] = dict(table_context)

    ppr = paragraph.find(f"{WORD_NS}pPr")
    style_id = _paragraph_style_id(ppr)
    style = styles.get(style_id or "", {})
    if style_id:
        record["style_id"] = style_id
    style_name = str(style.get("name") or "")
    if style_name:
        record["style_name"] = style_name

    outline_level = _paragraph_outline_level(ppr, style)
    if outline_level is not None:
        record["outline_level"] = outline_level
        record["heading_level"] = outline_level + 1
    else:
        heading_level = _heading_level_from_style(style_id, style_name)
        if heading_level is not None:
            record["heading_level"] = heading_level

    paragraph_numbering = _paragraph_numbering(ppr, style)
    numbering_record = _numbering_record(paragraph_numbering, numbering, numbering_state)
    if numbering_record:
        record["numbering"] = numbering_record
        label = str(numbering_record.get("label") or "").strip()
        if label:
            record["structure_label"] = label
        structure_number = _structure_number_from_label(label)
        if structure_number:
            record["structure_number"] = structure_number

    return record


def _read_styles(document: ZipFile) -> StyleDefinitions:
    try:
        root = _read_xml_part(document, "word/styles.xml")
    except DocxExtractionError:
        return {}

    styles: StyleDefinitions = {}
    for style in root.findall(f"{WORD_NS}style"):
        if _attr(style, "type") != "paragraph":
            continue
        style_id = _attr(style, "styleId")
        if not style_id:
            continue
        ppr = style.find(f"{WORD_NS}pPr")
        record: Dict[str, object] = {}
        name = _val(style.find(f"{WORD_NS}name"))
        if name:
            record["name"] = name
        outline_level = _outline_level(ppr)
        if outline_level is not None:
            record["outline_level"] = outline_level
        numbering = _num_pr(ppr)
        if numbering:
            record["numbering"] = numbering
        styles[style_id] = record
    return styles


def _read_numbering(document: ZipFile) -> NumberingDefinitions:
    try:
        root = _read_xml_part(document, "word/numbering.xml")
    except DocxExtractionError:
        return {}

    abstract: Dict[str, Dict[int, Dict[str, object]]] = {}
    for abstract_num in root.findall(f"{WORD_NS}abstractNum"):
        abstract_id = _attr(abstract_num, "abstractNumId")
        if not abstract_id:
            continue
        levels: Dict[int, Dict[str, object]] = {}
        for level in abstract_num.findall(f"{WORD_NS}lvl"):
            level_index = _int_or_none(_attr(level, "ilvl"))
            if level_index is None:
                continue
            levels[level_index] = {
                "start": _int_or_none(_val(level.find(f"{WORD_NS}start"))) or 1,
                "format": _val(level.find(f"{WORD_NS}numFmt")) or "decimal",
                "text": _val(level.find(f"{WORD_NS}lvlText")) or f"%{level_index + 1}.",
            }
        abstract[abstract_id] = levels

    nums: Dict[str, str] = {}
    for num in root.findall(f"{WORD_NS}num"):
        num_id = _attr(num, "numId")
        abstract_id = _val(num.find(f"{WORD_NS}abstractNumId"))
        if num_id and abstract_id:
            nums[num_id] = abstract_id

    return {"abstract": abstract, "nums": nums}


def _paragraph_style_id(ppr: ET.Element | None) -> str | None:
    return _val(ppr.find(f"{WORD_NS}pStyle")) if ppr is not None else None


def _paragraph_outline_level(ppr: ET.Element | None, style: Dict[str, object]) -> int | None:
    direct = _outline_level(ppr)
    if direct is not None:
        return direct
    style_outline = style.get("outline_level")
    return style_outline if isinstance(style_outline, int) else None


def _outline_level(ppr: ET.Element | None) -> int | None:
    if ppr is None:
        return None
    return _int_or_none(_val(ppr.find(f"{WORD_NS}outlineLvl")))


def _paragraph_numbering(ppr: ET.Element | None, style: Dict[str, object]) -> Dict[str, int | str] | None:
    direct = _num_pr(ppr)
    if direct:
        return direct
    style_numbering = style.get("numbering")
    return style_numbering if isinstance(style_numbering, dict) else None


def _num_pr(ppr: ET.Element | None) -> Dict[str, int | str] | None:
    if ppr is None:
        return None
    num_pr = ppr.find(f"{WORD_NS}numPr")
    if num_pr is None:
        return None
    num_id = _val(num_pr.find(f"{WORD_NS}numId"))
    level = _int_or_none(_val(num_pr.find(f"{WORD_NS}ilvl")))
    if not num_id:
        return None
    return {
        "num_id": num_id,
        "level": level if level is not None else 0,
    }


def _numbering_record(
    paragraph_numbering: Dict[str, int | str] | None,
    numbering: NumberingDefinitions,
    numbering_state: Dict[str, Dict[int, int]],
) -> Dict[str, object] | None:
    if not paragraph_numbering:
        return None
    num_id = str(paragraph_numbering.get("num_id") or "")
    level_index = int(paragraph_numbering.get("level") or 0)
    nums = numbering.get("nums") if isinstance(numbering, dict) else {}
    abstract = numbering.get("abstract") if isinstance(numbering, dict) else {}
    abstract_id = nums.get(num_id) if isinstance(nums, dict) else None
    levels = abstract.get(abstract_id) if isinstance(abstract, dict) and abstract_id is not None else None
    level_definition = levels.get(level_index) if isinstance(levels, dict) else None
    if not isinstance(level_definition, dict):
        return {
            "num_id": num_id,
            "level": level_index,
        }

    counters = numbering_state.setdefault(num_id, {})
    for tracked_level in list(counters):
        if tracked_level > level_index:
            del counters[tracked_level]
    start = int(level_definition.get("start") or 1)
    counters[level_index] = counters.get(level_index, start - 1) + 1
    for parent_level in range(level_index):
        if parent_level not in counters:
            parent_definition = levels.get(parent_level, {}) if isinstance(levels, dict) else {}
            counters[parent_level] = int(parent_definition.get("start") or 1)

    number_format = str(level_definition.get("format") or "decimal")
    level_text = str(level_definition.get("text") or f"%{level_index + 1}.")
    label = _render_numbering_label(level_text, counters, levels if isinstance(levels, dict) else {})
    return {
        "num_id": num_id,
        "level": level_index,
        "format": number_format,
        "level_text": level_text,
        "value": counters[level_index],
        "label": label,
    }


def _render_numbering_label(level_text: str, counters: Dict[int, int], levels: Dict[int, Dict[str, object]]) -> str:
    def replace(match: re.Match[str]) -> str:
        level_index = int(match.group(1)) - 1
        value = counters.get(level_index)
        if value is None:
            return ""
        level_definition = levels.get(level_index, {})
        return _format_number(value, str(level_definition.get("format") or "decimal"))

    return re.sub(r"%(\d+)", replace, level_text).strip()


def _format_number(value: int, number_format: str) -> str:
    if number_format == "lowerLetter":
        return _letters(value).lower()
    if number_format == "upperLetter":
        return _letters(value).upper()
    if number_format == "lowerRoman":
        return _roman(value).lower()
    if number_format == "upperRoman":
        return _roman(value).upper()
    return str(value)


def _letters(value: int) -> str:
    if value <= 0:
        return str(value)
    letters = []
    current = value
    while current:
        current -= 1
        letters.append(chr(ord("A") + (current % 26)))
        current //= 26
    return "".join(reversed(letters))


def _roman(value: int) -> str:
    if value <= 0:
        return str(value)
    numerals = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    ]
    result = []
    remaining = value
    for amount, numeral in numerals:
        while remaining >= amount:
            result.append(numeral)
            remaining -= amount
    return "".join(result)


def _structure_number_from_label(label: str) -> str:
    if not label:
        return ""
    stripped = re.sub(r"^[^\w]+|[^\w]+$", "", label).strip()
    if not stripped or len(stripped) > 40:
        return ""
    return stripped


def _heading_level_from_style(style_id: str | None, style_name: str) -> int | None:
    for value in (style_id or "", style_name or ""):
        match = re.search(r"heading\s*([1-9])", value, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _children(element: ET.Element, local_name: str) -> List[ET.Element]:
    return [child for child in list(element) if child.tag == f"{WORD_NS}{local_name}"]


def _attr(element: ET.Element, name: str) -> str:
    return str(element.get(f"{WORD_NS}{name}") or element.get(name) or "")


def _val(element: ET.Element | None) -> str:
    return _attr(element, "val") if element is not None else ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _supplemental_part_names(document: ZipFile) -> List[str]:
    return [
        name
        for name in document.namelist()
        if name.endswith(".xml") and any(name.startswith(prefix) for prefix in SUPPLEMENTAL_PART_PREFIXES)
    ]


def _paragraph_text(paragraph: ET.Element) -> str:
    parts = []
    for node in paragraph.iter():
        if node.tag == f"{WORD_NS}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{WORD_NS}tab":
            parts.append("\t")
        elif node.tag in {f"{WORD_NS}br", f"{WORD_NS}cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _source_part_label(part_name: str) -> str:
    return part_name.removeprefix("word/").removesuffix(".xml")


def _read_xml_part(document: ZipFile, part_name: str, missing_message: str | None = None) -> ET.Element:
    try:
        document_xml = document.read(part_name)
    except KeyError as exc:
        raise DocxExtractionError(missing_message or f"The Word document part {part_name} is missing.") from exc

    try:
        root = parse_docx_xml(document_xml, part_name=part_name)
    except (ET.ParseError, UnsafeDocxXmlError) as exc:
        raise DocxExtractionError(f"The Word document part {part_name} could not be read.") from exc
    return root
