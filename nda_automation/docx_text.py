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
DOCX_TOO_LARGE_MESSAGE = "The Word document is too large after decompression."
DOCX_SUSPICIOUS_COMPRESSION_MESSAGE = "The Word document uses a suspicious compression ratio."
SUPPLEMENTAL_PART_PREFIXES = (
    "word/header",
    "word/footer",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments.xml",
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
        with ZipFile(BytesIO(data)) as document:
            validate_docx_archive(document)
            paragraphs = _extract_main_document_paragraphs(document)
            paragraphs.extend(_extract_supplemental_paragraphs(document))
    except BadZipFile as exc:
        raise DocxExtractionError("The uploaded file is not a valid .docx document.") from exc

    if not paragraphs:
        raise DocxExtractionError("No readable text was found in the Word document.")
    return paragraphs


def validate_docx_archive(document: ZipFile) -> None:
    total_uncompressed = 0
    for item in document.infolist():
        if item.is_dir():
            continue
        total_uncompressed += item.file_size
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise DocxExtractionError(DOCX_TOO_LARGE_MESSAGE)
        if item.file_size and item.compress_size == 0:
            raise DocxExtractionError(DOCX_SUSPICIOUS_COMPRESSION_MESSAGE)
        if item.compress_size and item.file_size / item.compress_size > MAX_DOCX_ENTRY_COMPRESSION_RATIO:
            raise DocxExtractionError(DOCX_SUSPICIOUS_COMPRESSION_MESSAGE)
        if is_docx_xml_part(item.filename):
            try:
                reject_unsafe_docx_xml(document.read(item.filename), part_name=item.filename)
            except UnsafeDocxXmlError as exc:
                raise DocxExtractionError(str(exc)) from exc


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

    def walk(parent: ET.Element, table_context: Dict[str, int] | None = None):
        nonlocal table_counter
        for child in list(parent):
            if child.tag == f"{WORD_NS}p":
                yield child, table_context
            elif child.tag == f"{WORD_NS}tbl":
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
                        })
            else:
                yield from walk(child, table_context)

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
