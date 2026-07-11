from __future__ import annotations

from io import BytesIO
import re
from typing import Any, Dict, Iterable, List, NamedTuple
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from .docx_xml import R_NS, REL_NS, UnsafeDocxXmlError, is_docx_xml_part, parse_docx_xml, reject_unsafe_docx_xml

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Office Math Markup Language. An inline equation lives in an ``m:oMath`` subtree
# whose literal characters are carried by ``m:t`` leaves. The revision-aware text
# walk historically ignored this namespace entirely, so a clause containing an
# inline equation silently lost that text -- the AI then reviewed a truncated
# clause (C1). We harvest ``m:t`` as its literal characters so the equation text
# reaches the reviewer.
MATH_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
# The relationships namespace an ``r:id`` attribute lives in (on ``w:hyperlink``),
# and the package-relationships namespace the ``.rels`` part uses for its
# ``<Relationship>`` elements. Used to resolve a hyperlink's target URL (D3).
R_NS_BRACED = "{" + R_NS + "}"
REL_NS_BRACED = "{" + REL_NS + "}"
# Markup-Compatibility-and-Extensibility namespace. An ``mc:AlternateContent``
# block carries the SAME content twice -- an ``mc:Choice`` (the modern DrawingML
# representation of e.g. a text box) and an ``mc:Fallback`` (the legacy VML copy
# for old consumers). A conformant reader picks exactly one branch; taking both
# double-counts the text box's text (A2).
MC_NS = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
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
# ``source_kind`` marker for a paragraph extracted from a supplemental part
# (header/footer/footnotes/endnotes) rather than the main document body. The
# export reconstructs only ``word/document.xml`` (the body), so any gate that
# compares the export against the extracted text must scope the expected side
# to the body and exclude paragraphs carrying this marker.
SUPPLEMENTAL_SOURCE_KIND = "supplemental"
# Word footnote/endnote parts hold two book-keeping notes per part -- a
# ``separator`` and a ``continuationSeparator`` (usually w:id="-1"/"0") -- that
# carry the horizontal rule Word draws above continued notes, NOT reviewable
# content. They must never be surfaced as a real footnote.
FOOTNOTE_BOOKKEEPING_TYPES = {"separator", "continuationSeparator"}
DocxParagraph = Dict[str, object]
NumberingDefinitions = Dict[str, object]
StyleDefinitions = Dict[str, Dict[str, object]]
# Word paragraph styles inherit via ``<w:basedOn>``; the chain is normally 1-3
# deep (e.g. "Title" -> "Normal"). Cap the walk so a hand-crafted cyclic or
# absurdly long basedOn cannot spin the resolver.
MAX_STYLE_CHAIN_DEPTH = 32
DocDefaults = Dict[str, object]


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


TRACKED_CHANGES_WARNING_TYPE = "docx_unresolved_tracked_changes"
TRACKED_CHANGES_WARNING_MESSAGE = (
    "The Word document contains unresolved tracked changes. The review reflects the current "
    "in-force baseline (tracked insertions excluded, tracked deletions restored); the redlines "
    "must be accepted or rejected by a human before any verdict is acted on."
)


def detect_docx_tracked_changes(data: bytes) -> dict[str, object] | None:
    """Return an extraction-quality dict if the DOCX carries unresolved redlines.

    Detects tracked insertions (``w:ins``) and deletions (``w:del``) plus tracked
    MOVES (``w:moveTo`` counted as an insertion, ``w:moveFrom`` as a deletion) in
    the main body and reviewable supplemental parts. A Word "track moves" edit
    emits ``w:moveFrom``/``w:moveTo`` -- NOT ``w:ins``/``w:del`` -- so without the
    move branch a relocated clause tripped no tracked-changes gate and silently
    auto-cleared (A1). Returns ``None`` for a clean document
    so the caller attaches no quality block and raises no flag. Callers thread the
    returned dict through ``attach_document_source`` so the warning reaches the
    review surface and the document-level tracked-changes gate forces human
    review. Never raises: extraction has already succeeded by the time this runs,
    so an unreadable archive here degrades to "no tracked changes detected".
    """
    try:
        validate_docx_bytes_before_open(data)
        with ZipFile(BytesIO(data)) as document:
            validate_docx_archive(document)
            insertions = 0
            deletions = 0
            for part_name in ["word/document.xml", *_supplemental_part_names(document)]:
                try:
                    root = _read_xml_part(document, part_name)
                except DocxExtractionError:
                    continue
                for node in root.iter():
                    # ``w:moveTo`` is the destination of a tracked move (new text
                    # not yet in force, like ``w:ins``); ``w:moveFrom`` is the
                    # origin (still-in-force text being removed, like ``w:del``).
                    if node.tag in (f"{WORD_NS}ins", f"{WORD_NS}moveTo"):
                        insertions += 1
                    elif node.tag in (f"{WORD_NS}del", f"{WORD_NS}moveFrom"):
                        deletions += 1
    except (BadZipFile, RecursionError, DocxExtractionError):
        return None

    if not insertions and not deletions:
        return None
    return {
        "has_tracked_changes": True,
        "tracked_insertions": insertions,
        "tracked_deletions": deletions,
        "reviewed_state": "in_force_baseline",
        "warnings": [
            {
                "type": TRACKED_CHANGES_WARNING_TYPE,
                "message": TRACKED_CHANGES_WARNING_MESSAGE,
            }
        ],
    }


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
    styles, doc_defaults = _read_styles(document)
    numbering = _read_numbering(document)
    rels = _read_hyperlink_relationships(document, "word/_rels/document.xml.rels")
    numbering_state: Dict[str, Dict[int, int]] = {}
    footnote_texts = _read_note_texts(document, "word/footnotes.xml", "footnote")
    endnote_texts = _read_note_texts(document, "word/endnotes.xml", "endnote")
    comment_records = _read_comment_records(document)
    # Comment ranges (w:commentRangeStart/End) can span several paragraphs, so the
    # open-range set is carried across the body walk below rather than resolved
    # per-paragraph. This mutable set names every comment id whose range opened in
    # an earlier paragraph and has not yet closed.
    open_comment_ids: "Dict[str, None]" = {}
    paragraphs: List[DocxParagraph] = []
    for indexed in iter_indexed_body_paragraphs(root):
        text = _paragraph_text(indexed.paragraph)
        annotations = _paragraph_annotation_metadata(
            indexed.paragraph,
            text,
            footnote_texts=footnote_texts,
            endnote_texts=endnote_texts,
            comment_records=comment_records,
            open_comment_ids=open_comment_ids,
        )
        if text:
            record = _paragraph_record(
                indexed.paragraph,
                text,
                source_index=indexed.source_index,
                styles=styles,
                doc_defaults=doc_defaults,
                numbering=numbering,
                numbering_state=numbering_state,
                table_context=indexed.table_context,
                rels=rels,
            )
            if annotations.get("footnotes"):
                record["footnotes"] = annotations["footnotes"]
            if annotations.get("comments"):
                record["comments"] = annotations["comments"]
            paragraphs.append(record)
        else:
            # A blank Word-numbered paragraph still consumes the next number in its
            # ``(numId, ilvl)`` sequence, so we advance the shared counter here even
            # though we surface no clause. Without this the counter only advanced
            # inside ``_paragraph_record`` (text-only), and every following clause
            # numbered ONE LOW versus Word / the faithful surface (D10).
            _advance_numbering_for_empty_paragraph(
                indexed.paragraph,
                styles=styles,
                numbering=numbering,
                numbering_state=numbering_state,
            )
    return paragraphs


def _extract_supplemental_paragraphs(document: ZipFile) -> List[DocxParagraph]:
    paragraphs: List[DocxParagraph] = []
    styles, doc_defaults = _read_styles(document)
    for part_name in sorted(_supplemental_part_names(document)):
        root = _read_xml_part(document, part_name)
        source_part = _source_part_label(part_name)
        for paragraph in root.iter(f"{WORD_NS}p"):
            text = _paragraph_text(paragraph)
            if text:
                paragraphs.append(_paragraph_record(
                    paragraph,
                    text,
                    source_part=source_part,
                    styles=styles,
                    doc_defaults=doc_defaults,
                    numbering={},
                    numbering_state={},
                ))
    return paragraphs


def _read_optional_xml_part(document: ZipFile, part_name: str) -> ET.Element | None:
    """Parse a DOCX part if present, else ``None``.

    Footnotes / endnotes / comments are optional parts: a plain agreement has
    none, and the caller must degrade to "no annotations" rather than raise. The
    part still passes the same XXE / entity-expansion rejection every other part
    does (via ``_read_xml_part``); only a genuinely absent part returns ``None``.
    """
    if part_name not in document.namelist():
        return None
    return _read_xml_part(document, part_name)


def _read_note_texts(document: ZipFile, part_name: str, kind: str) -> Dict[str, Dict[str, str]]:
    """Map a foot/endnote id -> ``{"text", "kind"}`` for real (non-separator) notes.

    The ``separator`` / ``continuationSeparator`` book-keeping notes (the rule Word
    draws above continued notes) carry no reviewable content and are dropped. Text
    is collected revision-aware so a tracked change inside a note reads as the
    in-force baseline, exactly like body text.
    """
    root = _read_optional_xml_part(document, part_name)
    if root is None:
        return {}
    notes: Dict[str, Dict[str, str]] = {}
    for note in root:
        if note.tag != f"{WORD_NS}{kind}":
            continue
        note_type = _attr(note, "type").strip()
        if note_type in FOOTNOTE_BOOKKEEPING_TYPES:
            continue
        note_id = _attr(note, "id").strip()
        if not note_id:
            continue
        text = _note_text(note)
        if text:
            notes[note_id] = {"text": text, "kind": kind}
    return notes


def _note_text(note: ET.Element) -> str:
    """Join a note's paragraphs into one reviewable string (blank paragraphs dropped)."""
    lines: List[str] = []
    for paragraph in note.iter(f"{WORD_NS}p"):
        paragraph_text = _paragraph_text(paragraph)
        if paragraph_text:
            lines.append(paragraph_text)
    return "\n".join(lines).strip()


def _read_comment_records(document: ZipFile) -> Dict[str, Dict[str, str]]:
    """Map comment id -> ``{"author", "date", "text"}`` from ``word/comments.xml``.

    Read for DISPLAY only: comment text is deliberately kept out of the extracted
    body text and the verdict engine (a comment like "add a non-circumvention
    covenant" must not manufacture a clause hit the agreement never makes). It is
    surfaced to the reviewer as a margin annotation keyed to the ranged paragraph.
    """
    root = _read_optional_xml_part(document, "word/comments.xml")
    if root is None:
        return {}
    records: Dict[str, Dict[str, str]] = {}
    for comment in root.findall(f"{WORD_NS}comment"):
        comment_id = _attr(comment, "id").strip()
        if not comment_id:
            continue
        text = _note_text(comment)
        record: Dict[str, str] = {"text": text}
        author = _attr(comment, "author").strip()
        if author:
            record["author"] = author
        date = _attr(comment, "date").strip()
        if date:
            record["date"] = date
        records[comment_id] = record
    return records


class _ParagraphAnnotationScan(NamedTuple):
    """Positions of note references and comment-range markers within one paragraph.

    Offsets are character indexes into the paragraph's revision-aware in-force text
    (the same text ``_paragraph_text`` returns), so a marker can be placed inline on
    the display surface without ever mutating ``paragraph.text``.
    """

    note_refs: List[tuple]  # (offset, kind, note_id)
    comment_starts: List[tuple]  # (offset, comment_id)
    comment_ends: List[tuple]  # (offset, comment_id)
    comment_refs: List[str]  # comment_id


def _scan_paragraph_annotations(paragraph: ET.Element) -> _ParagraphAnnotationScan:
    """Walk a paragraph collecting note-reference and comment-range positions.

    Mirrors ``_collect_revision_aware_text`` exactly for text accounting -- it skips
    ``w:ins`` / ``w:moveTo`` (not in the in-force baseline) and takes the modern
    ``mc:Choice`` branch of an ``mc:AlternateContent`` -- so every recorded offset
    lines up with the character offsets of ``_paragraph_text``.
    """
    note_refs: List[tuple] = []
    comment_starts: List[tuple] = []
    comment_ends: List[tuple] = []
    comment_refs: List[str] = []
    offset = [0]

    def walk(node: ET.Element) -> None:
        tag = node.tag
        if tag in (f"{WORD_NS}ins", f"{WORD_NS}moveTo"):
            return
        if tag == f"{WORD_NS}t" or tag == f"{WORD_NS}delText":
            if node.text:
                offset[0] += len(node.text)
            return
        if tag in {f"{WORD_NS}tab", f"{WORD_NS}br", f"{WORD_NS}cr"}:
            offset[0] += 1
            return
        if tag == f"{WORD_NS}footnoteReference":
            note_refs.append((offset[0], "footnote", _attr(node, "id").strip()))
            return
        if tag == f"{WORD_NS}endnoteReference":
            note_refs.append((offset[0], "endnote", _attr(node, "id").strip()))
            return
        if tag == f"{WORD_NS}commentRangeStart":
            comment_starts.append((offset[0], _attr(node, "id").strip()))
            return
        if tag == f"{WORD_NS}commentRangeEnd":
            comment_ends.append((offset[0], _attr(node, "id").strip()))
            return
        if tag == f"{WORD_NS}commentReference":
            comment_refs.append(_attr(node, "id").strip())
            return

        children = list(node)
        if tag == f"{MC_NS}AlternateContent" and any(child.tag == f"{MC_NS}Choice" for child in children):
            children = [child for child in children if child.tag != f"{MC_NS}Fallback"]
        for child in children:
            walk(child)

    walk(paragraph)
    return _ParagraphAnnotationScan(note_refs, comment_starts, comment_ends, comment_refs)


def _paragraph_annotation_metadata(
    paragraph: ET.Element,
    text: str,
    *,
    footnote_texts: Dict[str, Dict[str, str]],
    endnote_texts: Dict[str, Dict[str, str]],
    comment_records: Dict[str, Dict[str, str]],
    open_comment_ids: Dict[str, None],
) -> Dict[str, List[dict]]:
    """Resolve a paragraph's footnote/endnote references and overlapping comments.

    Returns ``{"footnotes": [...], "comments": [...]}`` (either omitted when empty).
    ``open_comment_ids`` is MUTATED to carry unbalanced comment ranges into the next
    paragraph, so a comment spanning several paragraphs is associated with every
    paragraph its range covers -- not only the one holding its start.

    Nothing here touches ``text``: markers are additive display metadata carrying a
    character ``offset`` into ``text`` so the reader sees WHICH span the note or
    comment hangs off, while the reviewable text stays byte-identical.
    """
    scan = _scan_paragraph_annotations(paragraph)
    result: Dict[str, List[dict]] = {}

    footnotes: List[dict] = []
    for note_offset, kind, note_id in scan.note_refs:
        note = (footnote_texts if kind == "footnote" else endnote_texts).get(note_id)
        entry: dict = {"id": note_id, "kind": kind, "offset": note_offset}
        if note:
            entry["text"] = note["text"]
        footnotes.append(entry)
    if footnotes:
        result["footnotes"] = footnotes

    # Which comment ids touch THIS paragraph: any range that was already open on
    # entry, any that starts here, and any bare commentReference on it.
    starts_here = {comment_id for _offset, comment_id in scan.comment_starts if comment_id}
    ends_here = {comment_id for _offset, comment_id in scan.comment_ends if comment_id}
    touched: "Dict[str, None]" = {}
    for comment_id in open_comment_ids:
        touched.setdefault(comment_id, None)
    for _offset, comment_id in scan.comment_starts:
        if comment_id:
            touched.setdefault(comment_id, None)
    for comment_id in scan.comment_refs:
        if comment_id:
            touched.setdefault(comment_id, None)

    start_offsets = {comment_id: offset for offset, comment_id in scan.comment_starts if comment_id}
    end_offsets = {comment_id: offset for offset, comment_id in scan.comment_ends if comment_id}

    comments: List[dict] = []
    for comment_id in touched:
        record = comment_records.get(comment_id)
        if record is None:
            continue
        entry = {"id": comment_id, "text": record.get("text", "")}
        if record.get("author"):
            entry["author"] = record["author"]
        if record.get("date"):
            entry["date"] = record["date"]
        start_offset = start_offsets.get(comment_id)
        if start_offset is not None:
            entry["offset"] = start_offset
        # When both ends of the range sit in THIS paragraph, carry the exact quoted
        # span so the reviewer sees precisely what the counterparty flagged.
        if comment_id in start_offsets and comment_id in end_offsets:
            span_start = start_offsets[comment_id]
            span_end = end_offsets[comment_id]
            if 0 <= span_start <= span_end <= len(text):
                quoted = text[span_start:span_end].strip()
                if quoted:
                    entry["quoted_text"] = quoted
        comments.append(entry)
    if comments:
        result["comments"] = comments

    # Advance the cross-paragraph open set: add ranges opened here, drop ones closed
    # here. A range closed in the same paragraph it opened never becomes "open".
    for comment_id in starts_here:
        if comment_id not in ends_here:
            open_comment_ids.setdefault(comment_id, None)
    for comment_id in ends_here:
        open_comment_ids.pop(comment_id, None)

    return result


class IndexedBodyParagraph(NamedTuple):
    """A body ``<w:p>`` with its canonical 1-based ``source_index``.

    This is the SINGLE source of truth for "number every body paragraph in document
    order". Both the review-paragraph ``source_index`` (minted in
    ``_extract_main_document_paragraphs``) and the export's physical-paragraph index
    (``docx_export._indexed_source_paragraphs``) are derived from this one walk, so a
    redline's ``source_index`` is an exact, twin-safe lookup into the same numbering
    the export applies. ``parent`` is the element the ``<w:p>`` is a direct child of
    (the export replaces/inserts in place); ``table_context`` carries cell
    coordinates when the paragraph lives in a table cell, else None.
    """

    source_index: int
    parent: ET.Element
    paragraph: ET.Element
    table_context: Dict[str, object] | None


def iter_indexed_body_paragraphs(root: ET.Element) -> Iterable[IndexedBodyParagraph]:
    """Yield every body ``<w:p>`` in canonical document order with a 1-based index.

    ``root`` may be the parsed ``word/document.xml`` element or a ``<w:body>``; the
    body is located when present so the numbering is identical regardless of which
    the caller passes.
    """
    body = root.find(f"{WORD_NS}body")
    container = body if body is not None else root
    table_counter = 0
    source_index = 0

    def walk(parent: ET.Element, table_context: Dict[str, object] | None, table_depth: int):
        nonlocal table_counter, source_index
        for child in list(parent):
            if child.tag == f"{WORD_NS}p":
                source_index += 1
                yield IndexedBodyParagraph(
                    source_index=source_index,
                    parent=parent,
                    paragraph=child,
                    table_context=table_context,
                )
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
                        cell_context: Dict[str, object] = {
                            "table_index": table_index,
                            "row_index": row_index,
                            "cell_index": cell_index,
                        }
                        cell_style = _table_cell_style(cell)
                        if cell_style:
                            cell_context["cell_style"] = cell_style
                        yield from walk(cell, {
                            **cell_context,
                        }, nested_table_depth)
            else:
                yield from walk(child, table_context, table_depth)

    yield from walk(container, None, 0)


def _iter_document_paragraphs(root: ET.Element) -> Iterable[tuple[ET.Element, Dict[str, object] | None]]:
    for indexed in iter_indexed_body_paragraphs(root):
        yield indexed.paragraph, indexed.table_context


def _paragraph_record(
    paragraph: ET.Element,
    text: str,
    *,
    styles: StyleDefinitions,
    numbering: NumberingDefinitions,
    numbering_state: Dict[str, Dict[int, int]],
    doc_defaults: DocDefaults | None = None,
    source_index: int | None = None,
    source_part: str | None = None,
    table_context: Dict[str, object] | None = None,
    rels: Dict[str, str] | None = None,
) -> DocxParagraph:
    record: DocxParagraph = {"text": text}
    if source_index is not None:
        record["source_index"] = source_index
    if source_part:
        record["source_part"] = source_part
    record["source_kind"] = "table_cell" if table_context else (SUPPLEMENTAL_SOURCE_KIND if source_part else "paragraph")
    if table_context:
        record["table"] = dict(table_context)

    runs = _paragraph_runs(paragraph, text, rels)
    if runs is not None:
        record["runs"] = runs

    ppr = paragraph.find(f"{WORD_NS}pPr")

    # Paragraph reading direction (D4). A ``<w:pPr>/<w:bidi>`` toggle marks a
    # right-to-left paragraph; without it Word / the faithful surface render RTL
    # but our reconstruction painted LTR. Emitted as a display-only field the
    # renderer maps to ``dir="rtl"``; the STORED text is never reordered, so the
    # outbound redline is unaffected. Absent (byte-identical) on LTR paragraphs.
    direction = _paragraph_direction(ppr)
    if direction:
        record["direction"] = direction
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

    indent_left = _paragraph_indent_left_points(ppr, paragraph_numbering, numbering)
    if indent_left is not None:
        record["indent_left"] = indent_left

    alignment = _paragraph_alignment(ppr, styles, style_id, doc_defaults)
    if alignment:
        record["alignment"] = alignment

    font = _paragraph_font(paragraph, styles, style_id, doc_defaults)
    if font:
        record["font"] = font

    font_size = _paragraph_font_size(paragraph, ppr)
    if font_size is not None:
        record["fontSize"] = font_size

    return record


def _advance_numbering_for_empty_paragraph(
    paragraph: ET.Element,
    *,
    styles: StyleDefinitions,
    numbering: NumberingDefinitions,
    numbering_state: Dict[str, Dict[int, int]],
) -> None:
    """Consume the next number for a blank numbered ``<w:p>`` WITHOUT surfacing a clause.

    Word counts an empty numbered paragraph: it takes the next value in its
    ``(numId, ilvl)`` sequence, so every following clause numbers one higher. Our
    extractor drops empty paragraphs entirely, so without this the shared counter
    never advanced and each following clause numbered ONE LOW versus Word / the
    faithful (docx-preview) surface -- wrong in BOTH the reconstruction ``::before``
    and the Structure tab (D10). We resolve the paragraph's numbering exactly as
    ``_paragraph_record`` does and call ``_numbering_record`` purely for its counter
    side effect; the returned label is discarded so no phantom empty clause reaches
    any surface (the number never becomes ``paragraph.text``). A blank paragraph with
    no numbering is a no-op.

    The counter is keyed per ``(numId, ilvl)`` (see ``numbering_state``), so advancing
    an empty item on one list never disturbs an unrelated list's sequence -- e.g. a
    document with 14 clauses numbered 1-14 on one list plus 4 empty items on another
    list keeps its 1-14 contiguous."""
    ppr = paragraph.find(f"{WORD_NS}pPr")
    style_id = _paragraph_style_id(ppr)
    style = styles.get(style_id or "", {})
    paragraph_numbering = _paragraph_numbering(ppr, style)
    if not paragraph_numbering:
        return
    _numbering_record(paragraph_numbering, numbering, numbering_state)


def _table_cell_style(cell: ET.Element) -> Dict[str, object]:
    tcpr = cell.find(f"{WORD_NS}tcPr")
    if tcpr is None:
        return {}
    style: Dict[str, object] = {}
    background_color = _table_cell_background_color(tcpr)
    if background_color:
        style["background_color"] = background_color
    width = _table_cell_width(tcpr)
    if width:
        style["width"] = width
    return style


def _table_cell_background_color(tcpr: ET.Element) -> str:
    shading = tcpr.find(f"{WORD_NS}shd")
    if shading is None:
        return ""
    fill = _attr(shading, "fill").strip()
    if not fill or fill.lower() in {"auto", "none"}:
        return ""
    if re.fullmatch(r"[0-9A-Fa-f]{6}", fill):
        return f"#{fill.lower()}"
    return ""


def _table_cell_width(tcpr: ET.Element) -> Dict[str, object]:
    width = tcpr.find(f"{WORD_NS}tcW")
    if width is None:
        return {}
    value = _int_or_none(_attr(width, "w"))
    width_type = _attr(width, "type").strip().lower()
    record: Dict[str, object] = {}
    if value is not None and value > 0:
        record["value"] = value
    if width_type:
        record["type"] = width_type
    return record


def _jc_alignment(ppr: ET.Element | None) -> str | None:
    """Map a ``<w:pPr>/<w:jc w:val>`` to left/center/right/justify, or ``None``.

    Word's ``both`` is justified text; ``start``/``end`` are the bidi-aware
    aliases for left/right. Shared by the inline paragraph reader and the style /
    docDefaults readers so the same mapping applies wherever a ``<w:jc>`` lives."""
    if ppr is None:
        return None
    value = _val(ppr.find(f"{WORD_NS}jc")).strip().lower()
    if not value:
        return None
    if value == "both":
        return "justify"
    if value in {"left", "center", "right", "justify"}:
        return value
    if value == "start":
        return "left"
    if value == "end":
        return "right"
    return None


def _baseline_runs(paragraph: ET.Element) -> Iterable[ET.Element]:
    """Yield the paragraph's runs that are IN FORCE in the baseline, in document order.

    Skips any run inside a tracked insertion (``w:ins``) or the destination of a
    tracked move (``w:moveTo``): that text is not yet part of the agreement, so
    its presentational properties (font/size) must not win the paragraph cascade.
    Without this a tracked INSERTION in the counterparty's font could make the
    whole baseline paragraph report that font (D1). Runs inside ``w:del`` /
    ``w:moveFrom`` (still in force) and plain runs are yielded, exactly mirroring
    how ``_collect_revision_aware_text`` treats each region.

    On a clean paragraph (no ``w:ins``/``w:moveTo``) this yields the same runs, in
    the same pre-order, that ``paragraph.iter(w:r)`` did -- so the resolved font /
    size is unchanged for every non-revised document."""
    def walk(node: ET.Element) -> Iterable[ET.Element]:
        for child in node:
            if child.tag in (f"{WORD_NS}ins", f"{WORD_NS}moveTo"):
                continue
            if child.tag == f"{WORD_NS}r":
                yield child
            yield from walk(child)

    yield from walk(paragraph)


def _inline_paragraph_font(paragraph: ET.Element) -> str | None:
    """The paragraph's dominant-run INLINE font name (``<w:rPr>/<w:rFonts w:ascii>``).

    Reuses the same first-run-with-rPr "dominant run" heuristic the redline
    emitter uses, so the captured from-state font matches what carries through a
    tracked change. Runs inside a tracked insertion are ignored (see
    ``_baseline_runs``) so the baseline paragraph does not adopt an inserted run's
    font. Only returned when a run names an explicit face."""
    for run in _baseline_runs(paragraph):
        rpr = run.find(f"{WORD_NS}rPr")
        if rpr is None:
            continue
        ascii_font = _run_font(rpr)
        if ascii_font:
            return ascii_font
    return None


def _resolve_style_chain_string(
    styles: StyleDefinitions,
    style_id: str | None,
    key: str,
) -> str | None:
    """First non-empty string ``key`` (e.g. "alignment"/"font") on the paragraph's
    style, walking ``<w:basedOn>`` toward the root.

    Word resolves inherited properties up the ``basedOn`` chain (a "Title" style
    that only sets centering still inherits "Normal"'s font). We stop at the first
    ancestor that defines ``key``. A visited set + hard depth cap make a cyclic or
    pathological chain terminate."""
    if not style_id or not isinstance(styles, dict):
        return None
    seen: set[str] = set()
    current: str | None = style_id
    depth = 0
    while current and current not in seen and depth < MAX_STYLE_CHAIN_DEPTH:
        seen.add(current)
        depth += 1
        record = styles.get(current)
        if not isinstance(record, dict):
            break
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
        based_on = record.get("based_on")
        current = based_on if isinstance(based_on, str) and based_on else None
    return None


def _paragraph_alignment(
    ppr: ET.Element | None,
    styles: StyleDefinitions,
    style_id: str | None,
    doc_defaults: DocDefaults | None,
) -> str | None:
    """The paragraph's EFFECTIVE alignment, mapped to left/center/right/justify.

    Resolves Word's cascade conservatively: an inline ``<w:pPr>/<w:jc>`` always
    wins; only when it is absent do we fall back to the paragraph style's own
    ``<w:jc>`` (walking ``basedOn``) and finally the document default
    (``<w:docDefaults>/<w:pPrDefault>/<w:pPr>/<w:jc>``). Returned only when some
    layer sets it, so the field stays additive and absent on left-default text."""
    inline = _jc_alignment(ppr)
    if inline:
        return inline
    style_value = _resolve_style_chain_string(styles, style_id, "alignment")
    if style_value:
        return style_value
    default = doc_defaults.get("alignment") if isinstance(doc_defaults, dict) else None
    return default if isinstance(default, str) and default else None


def _paragraph_font(
    paragraph: ET.Element,
    styles: StyleDefinitions,
    style_id: str | None,
    doc_defaults: DocDefaults | None,
) -> str | None:
    """The paragraph's EFFECTIVE base font name.

    Resolves Word's cascade conservatively: an inline run ``<w:rFonts w:ascii>``
    always wins (dominant-run heuristic); only when no run names a face do we fall
    back to the paragraph style's ``<w:rPr>/<w:rFonts>`` (walking ``basedOn``) and
    finally the document default (``<w:docDefaults>/<w:rPrDefault>/<w:rPr>/<w:rFonts>``).
    Returned only when some layer names a font, so the field stays additive."""
    inline = _inline_paragraph_font(paragraph)
    if inline:
        return inline
    style_value = _resolve_style_chain_string(styles, style_id, "font")
    if style_value:
        return style_value
    default = doc_defaults.get("font") if isinstance(doc_defaults, dict) else None
    return default if isinstance(default, str) and default else None


def _paragraph_font_size(paragraph: ET.Element, ppr: ET.Element | None) -> int | None:
    """The paragraph's from-state font size in whole points, or ``None``.

    Prefers the paragraph-mark run-default size (``<w:pPr>/<w:rPr>/<w:sz>``),
    which Word applies to the whole paragraph mark; otherwise falls back to the
    dominant-run size using the same first-run-with-rPr heuristic as
    ``_paragraph_font``. Runs inside a tracked insertion are ignored (see
    ``_baseline_runs``) so an inserted run's size cannot win the baseline. Only
    returned when present, so the field is additive."""
    if ppr is not None:
        mark_rpr = ppr.find(f"{WORD_NS}rPr")
        if mark_rpr is not None:
            mark_size = _run_size(mark_rpr)
            if mark_size is not None:
                return mark_size

    for run in _baseline_runs(paragraph):
        rpr = run.find(f"{WORD_NS}rPr")
        if rpr is None:
            continue
        size = _run_size(rpr)
        if size is not None:
            return size
    return None


def _read_styles(document: ZipFile) -> tuple[StyleDefinitions, DocDefaults]:
    """Parse ``word/styles.xml`` into (paragraph-style records, document defaults).

    Each style record additionally carries the presentational facts a paragraph
    can inherit -- ``based_on`` (the ``<w:basedOn>`` parent, so the cascade can be
    walked), ``alignment`` (the style's ``<w:pPr>/<w:jc>``) and ``font`` (the
    style's ``<w:rPr>/<w:rFonts w:ascii>``) -- so ``_paragraph_alignment`` /
    ``_paragraph_font`` can fall back to the style when the paragraph sets nothing
    inline. The document-wide defaults (``<w:docDefaults>``) are returned
    separately as the bottom of that cascade."""
    try:
        root = _read_xml_part(document, "word/styles.xml")
    except DocxExtractionError:
        return {}, {}

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
        based_on = _val(style.find(f"{WORD_NS}basedOn")).strip()
        if based_on:
            record["based_on"] = based_on
        outline_level = _outline_level(ppr)
        if outline_level is not None:
            record["outline_level"] = outline_level
        numbering = _num_pr(ppr)
        if numbering:
            record["numbering"] = numbering
        alignment = _jc_alignment(ppr)
        if alignment:
            record["alignment"] = alignment
        style_font = _run_font(style.find(f"{WORD_NS}rPr"))
        if style_font:
            record["font"] = style_font
        styles[style_id] = record
    return styles, _read_doc_defaults(root)


def _read_doc_defaults(styles_root: ET.Element) -> DocDefaults:
    """The document-wide run/paragraph defaults from ``<w:docDefaults>``.

    Word's inheritance bottoms out here: a run with no explicit font and no style
    font still renders in ``<w:rPrDefault>/<w:rPr>/<w:rFonts w:ascii>``, and a
    document may set a default justification in ``<w:pPrDefault>/<w:pPr>/<w:jc>``.
    Only present keys are returned, so the fallback stays additive."""
    defaults = styles_root.find(f"{WORD_NS}docDefaults")
    if defaults is None:
        return {}
    record: DocDefaults = {}
    rpr_default = defaults.find(f"{WORD_NS}rPrDefault")
    if rpr_default is not None:
        default_font = _run_font(rpr_default.find(f"{WORD_NS}rPr"))
        if default_font:
            record["font"] = default_font
    ppr_default = defaults.find(f"{WORD_NS}pPrDefault")
    if ppr_default is not None:
        default_alignment = _jc_alignment(ppr_default.find(f"{WORD_NS}pPr"))
        if default_alignment:
            record["alignment"] = default_alignment
    return record


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
            levels[level_index] = _read_level_definition(level, level_index)
        abstract[abstract_id] = levels

    nums: Dict[str, str] = {}
    # ``<w:lvlOverride>`` restarts / re-defines a level for ONE ``<w:num>`` instance
    # (restart-at-N lists, per-instance numFmt/lvlText). Keyed numId -> ilvl ->
    # {"start_override": int?, "level": {...}?} so ``_numbering_record`` can layer the
    # override on top of the shared abstract definition instead of ignoring it (D11).
    overrides: Dict[str, Dict[int, Dict[str, object]]] = {}
    for num in root.findall(f"{WORD_NS}num"):
        num_id = _attr(num, "numId")
        abstract_id = _val(num.find(f"{WORD_NS}abstractNumId"))
        if not (num_id and abstract_id):
            continue
        nums[num_id] = abstract_id
        num_overrides = _read_level_overrides(num)
        if num_overrides:
            overrides[num_id] = num_overrides

    return {"abstract": abstract, "nums": nums, "overrides": overrides}


def _read_level_definition(level: ET.Element, level_index: int) -> Dict[str, object]:
    """Parse a ``<w:lvl>`` into the level record the numbering counter consumes.

    Shared by the abstract-numbering reader and the per-instance ``<w:lvlOverride>``
    reader (D11) so an override carrying a full ``<w:lvl>`` is parsed identically to
    the abstract level it replaces. Also captures the level's left indent (twips) so
    a paragraph that numbers at this level but carries no direct ``<w:ind>`` can
    still resolve its effective indentation from the numbering definition."""
    record: Dict[str, object] = {
        "start": _int_or_none(_val(level.find(f"{WORD_NS}start"))) or 1,
        "format": _val(level.find(f"{WORD_NS}numFmt")) or "decimal",
        "text": _val(level.find(f"{WORD_NS}lvlText")) or f"%{level_index + 1}.",
    }
    indent_left = _indent_left_twips(level.find(f"{WORD_NS}pPr"))
    if indent_left is not None:
        record["indent_left"] = indent_left
    return record


def _read_level_overrides(num: ET.Element) -> Dict[int, Dict[str, object]]:
    """Read the ``<w:lvlOverride>`` entries of one ``<w:num>`` instance (D11).

    Each override targets a level (``w:ilvl``) and may carry a ``<w:startOverride>``
    (restart that level's counter at N for this instance) and/or a full ``<w:lvl>``
    that re-defines the level (numFmt/lvlText/start) for this instance only. Returns
    ilvl -> {"start_override": int?, "level": {...}?}; empty overrides are dropped so
    the map stays sparse and the no-override path is untouched."""
    overrides: Dict[int, Dict[str, object]] = {}
    for override in num.findall(f"{WORD_NS}lvlOverride"):
        level_index = _int_or_none(_attr(override, "ilvl"))
        if level_index is None:
            continue
        record: Dict[str, object] = {}
        start_override = _int_or_none(_val(override.find(f"{WORD_NS}startOverride")))
        if start_override is not None:
            record["start_override"] = start_override
        level = override.find(f"{WORD_NS}lvl")
        if level is not None:
            record["level"] = _read_level_definition(level, level_index)
        if record:
            overrides[level_index] = record
    return overrides


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


def _indent_left_twips(ppr: ET.Element | None) -> int | None:
    """The left indent from ``<w:pPr>/<w:ind w:left>`` in twips, or ``None``.

    Word stores indentation in twentieths of a point (twips). Returned raw (twips)
    so callers can resolve the effective indent before converting to points."""
    if ppr is None:
        return None
    ind = ppr.find(f"{WORD_NS}ind")
    if ind is None:
        return None
    return _int_or_none(_attr(ind, "left"))


def _paragraph_indent_left_points(
    ppr: ET.Element | None,
    paragraph_numbering: Dict[str, int | str] | None,
    numbering: NumberingDefinitions,
) -> int | None:
    """The paragraph's effective left indent in whole points, or ``None``.

    Prefers the paragraph's own ``<w:pPr>/<w:ind w:left>``; otherwise falls back to
    the indent stored on the numbering level the paragraph references (numId/ilvl),
    which is how sub-clauses get their indentation even when they sit at ilvl 0.
    Twips are converted to points (``round(twips / 20)``). Returned only when the
    resolved indent is greater than zero, so the field stays purely additive."""
    twips = _indent_left_twips(ppr)
    if twips is None and paragraph_numbering:
        twips = _numbering_level_indent_twips(paragraph_numbering, numbering)
    if twips is None or twips <= 0:
        return None
    return int(round(twips / 20))


def _numbering_level_indent_twips(
    paragraph_numbering: Dict[str, int | str],
    numbering: NumberingDefinitions,
) -> int | None:
    num_id = str(paragraph_numbering.get("num_id") or "")
    level_index = int(paragraph_numbering.get("level") or 0)
    nums = numbering.get("nums") if isinstance(numbering, dict) else {}
    abstract = numbering.get("abstract") if isinstance(numbering, dict) else {}
    abstract_id = nums.get(num_id) if isinstance(nums, dict) else None
    levels = abstract.get(abstract_id) if isinstance(abstract, dict) and abstract_id is not None else None
    level_definition = levels.get(level_index) if isinstance(levels, dict) else None
    if not isinstance(level_definition, dict):
        return None
    indent_left = level_definition.get("indent_left")
    return indent_left if isinstance(indent_left, int) else None


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
    """Compute a paragraph's Word-canonical autonumber (value + rendered label).

    NUMBERING RECONCILIATION RULE (D13) -- the single source both display surfaces
    resolve against:

      * The ``label`` this returns is what the RECONSTRUCTION surface paints via the
        ``data-structure-label`` attribute + a CSS ``::before`` (see
        ``redline-rendering.paragraphStructureAttributes``); it is NEVER emitted as a
        text node, so a numbering change here can never alter ``paragraph.text`` or
        the outbound redline.
      * The FAITHFUL surface (``docx-faithful-render.js`` -> the vendored docx-preview
        library) computes numbering itself, directly from ``word/numbering.xml``, and
        is authoritative for what Word actually prints.

    The two agree ONLY if this function reproduces Word's counting rules. So this is
    where the reconciliation lives: empty numbered paragraphs advance the counter
    (D10, via ``_advance_numbering_for_empty_paragraph``), ``<w:lvlOverride>`` /
    ``<w:startOverride>`` restarts are honored (D11, via
    ``_effective_numbering_levels``), and custom ``lvlText`` templates render into the
    label verbatim. docx-preview is not modified; instead OUR engine is made
    Word-canonical so both surfaces show the same number for the same clause."""
    if not paragraph_numbering:
        return None
    num_id = str(paragraph_numbering.get("num_id") or "")
    level_index = int(paragraph_numbering.get("level") or 0)
    nums = numbering.get("nums") if isinstance(numbering, dict) else {}
    abstract = numbering.get("abstract") if isinstance(numbering, dict) else {}
    overrides = numbering.get("overrides") if isinstance(numbering, dict) else {}
    abstract_id = nums.get(num_id) if isinstance(nums, dict) else None
    levels = abstract.get(abstract_id) if isinstance(abstract, dict) and abstract_id is not None else None
    num_overrides = overrides.get(num_id) if isinstance(overrides, dict) else None

    # Layer any per-instance ``<w:lvlOverride>`` (restart-at-N / re-defined level) on
    # top of the shared abstract levels so BOTH the counter and the rendered label
    # honor the override. With no overrides this is the abstract levels verbatim, so
    # the common path is unchanged (D11).
    effective_levels = _effective_numbering_levels(levels, num_overrides)
    level_definition = effective_levels.get(level_index)
    if not isinstance(level_definition, dict) or not level_definition:
        return {
            "num_id": num_id,
            "level": level_index,
        }

    counters = numbering_state.setdefault(num_id, {})
    for tracked_level in list(counters):
        if tracked_level > level_index:
            del counters[tracked_level]
    # ``start`` carries any ``<w:startOverride>`` (folded in by
    # ``_effective_numbering_levels``), so the first use of this level in this num
    # instance restarts at the overridden value and increments normally after (D11).
    start = int(level_definition.get("start") or 1)
    counters[level_index] = counters.get(level_index, start - 1) + 1
    for parent_level in range(level_index):
        if parent_level not in counters:
            parent_definition = effective_levels.get(parent_level, {})
            counters[parent_level] = int(parent_definition.get("start") or 1)

    number_format = str(level_definition.get("format") or "decimal")
    level_text = str(level_definition.get("text") or f"%{level_index + 1}.")
    label = _render_numbering_label(level_text, counters, effective_levels)
    return {
        "num_id": num_id,
        "level": level_index,
        "format": number_format,
        "level_text": level_text,
        "value": counters[level_index],
        "label": label,
    }


def _effective_numbering_levels(
    levels: Dict[int, Dict[str, object]] | None,
    num_overrides: Dict[int, Dict[str, object]] | None,
) -> Dict[int, Dict[str, object]]:
    """Merge a ``<w:num>`` instance's ``<w:lvlOverride>`` entries onto its abstract levels.

    A ``<w:startOverride>`` replaces the level's ``start`` (so the counter restarts
    at N for this instance); a full ``<w:lvl>`` inside the override replaces the
    level's format/text/start wholesale. Levels with neither an abstract definition
    nor an override are absent. With ``num_overrides`` empty/None the result is a
    faithful copy of ``levels`` so the no-override path behaves exactly as before (D11)."""
    merged: Dict[int, Dict[str, object]] = {}
    indices: set[int] = set()
    if isinstance(levels, dict):
        indices.update(levels.keys())
    if isinstance(num_overrides, dict):
        indices.update(num_overrides.keys())
    for index in indices:
        base = levels.get(index) if isinstance(levels, dict) else None
        record: Dict[str, object] = dict(base) if isinstance(base, dict) else {}
        override = num_overrides.get(index) if isinstance(num_overrides, dict) else None
        if isinstance(override, dict):
            override_level = override.get("level")
            if isinstance(override_level, dict):
                record.update(override_level)
            start_override = override.get("start_override")
            if isinstance(start_override, int):
                record["start"] = start_override
        if record:
            merged[index] = record
    return merged


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
    parts: List[str] = []
    _collect_revision_aware_text(paragraph, parts)
    return "".join(parts).strip()


def _collect_revision_aware_text(node: ET.Element, parts: List[str]) -> None:
    """Append a node's reviewable text, honoring tracked-change markup.

    The reviewed state is the CURRENT in-force baseline: tracked *insertions*
    (``w:ins``, and the destination ``w:moveTo`` of a tracked move) are not yet
    part of the agreement, so their text is skipped; tracked *deletions*
    (``w:del`` carrying ``w:delText``) and the *origin* of a tracked move
    (``w:moveFrom``, whose runs are still in force) are restored. Dropping
    ``w:moveTo`` while keeping ``w:moveFrom`` removes the DUPLICATE a move
    otherwise produces (the clause would appear at both its old and new location)
    and leaves the moved clause exactly once, at its still-in-force origin (A1).
    This mirrors ``docx_health`` (which already reads ``w:delText``) and replaces
    the old flat ``w:t``-only walk that silently yielded the "all insertions
    accepted, all deletions gone" hypothetical.

    An ``mc:AlternateContent`` block stores the same content twice (a modern
    ``mc:Choice`` and a legacy ``mc:Fallback``); a conformant reader takes one
    branch, so we recurse into ``mc:Choice`` and skip ``mc:Fallback`` -- otherwise
    a text box's text is counted twice (A2).

    A clean document has no ``w:ins``/``w:del``/``w:delText``/move/AlternateContent
    markup anywhere, so this recursion visits exactly the same
    ``w:t``/``w:tab``/``w:br``/``w:cr`` nodes, in document order, that the previous
    ``.iter()`` walk did -- byte-identical output.
    ``presence_of_tracked_changes`` flags the revision case separately.
    """
    tag = node.tag
    if tag in (f"{WORD_NS}ins", f"{WORD_NS}moveTo"):
        # Tracked insertion / move destination: not in the in-force baseline yet,
        # drop its text. (``w:moveFrom`` is NOT dropped -- its runs are still in
        # force, so it falls through to the generic recursion below and is kept.)
        return
    if tag == f"{WORD_NS}t":
        if node.text:
            parts.append(node.text)
    elif tag == f"{WORD_NS}delText":
        # Tracked-deleted text is still in force in the baseline; restore it.
        if node.text:
            parts.append(node.text)
    elif tag == f"{MATH_NS}t":
        # OMML equation text (``m:oMath`` -> ``m:r`` -> ``m:t``). The extractor
        # historically walked only ``w:t``/``w:delText``, so an inline equation's
        # characters were dropped and a clause carrying one reached the reviewer
        # TRUNCATED (C1). Harvest the equation's literal characters so the clause
        # text is complete. A document with no equations has no ``m:t`` node, so
        # this branch never fires there -- the walk stays byte-identical.
        if node.text:
            parts.append(node.text)
    elif tag == f"{WORD_NS}tab":
        parts.append("\t")
    elif tag in {f"{WORD_NS}br", f"{WORD_NS}cr"}:
        parts.append("\n")

    children = list(node)
    if tag == f"{MC_NS}AlternateContent":
        # Honor markup-compatibility: when a modern ``mc:Choice`` is present, the
        # legacy ``mc:Fallback`` is a redundant copy of the same content -- skip it
        # so the text box's text is collected once, not twice.
        if any(child.tag == f"{MC_NS}Choice" for child in children):
            children = [child for child in children if child.tag != f"{MC_NS}Fallback"]
    for child in children:
        _collect_revision_aware_text(child, parts)


def _paragraph_runs(
    paragraph: ET.Element,
    text: str,
    rels: Dict[str, str] | None = None,
) -> List[Dict[str, object]] | None:
    """Build a run-level breakdown of the paragraph with bold/italic/underline.

    Returns ``None`` (and the caller keeps only the flat ``text``) unless at least
    one run carries formatting (or a hyperlink target) AND the reconstructed run
    text exactly matches the paragraph ``text``. The strict match keeps ``runs``
    purely additive: any paragraph whose runs cannot be faithfully reconstructed
    (unusual nesting, fields, etc.) falls back to the flat-text rendering with no
    fidelity loss.

    ``rels`` maps a relationship id to its target URL so a run inside a
    ``<w:hyperlink r:id=...>`` carries the resolved ``hyperlink`` target (D3). A
    paragraph with no hyperlink threads ``href=None`` for every run, so the
    ``hyperlink`` key is never added and the breakdown is byte-identical to the
    old ``paragraph.iter(w:r)`` walk.
    """
    runs: List[Dict[str, object]] = []
    any_formatted = False
    for run, href in _iter_runs_with_hyperlink(paragraph, rels or {}):
        run_text = _run_text(run)
        if not run_text:
            continue
        formatting = _run_formatting(run)
        if href:
            formatting["hyperlink"] = href
        if (
            formatting["bold"]
            or formatting["italic"]
            or formatting["underline"]
            or formatting.get("color")
            or formatting.get("highlight")
            or formatting.get("strike")
            or formatting.get("vertAlign")
            or formatting.get("hyperlink")
        ):
            any_formatted = True
        if runs and _run_formatting_matches(runs[-1], formatting):
            runs[-1]["text"] = str(runs[-1]["text"]) + run_text
        else:
            runs.append({"text": run_text, **formatting})

    if not any_formatted or not runs:
        return None

    if "".join(str(run["text"]) for run in runs).strip() != text:
        return None
    return _trim_run_edges(runs)


def _run_text(run: ET.Element) -> str:
    parts: List[str] = []
    _collect_revision_aware_text(run, parts)
    return "".join(parts)


def _iter_runs_with_hyperlink(
    paragraph: ET.Element,
    rels: Dict[str, str],
) -> Iterable[tuple[ET.Element, str | None]]:
    """Yield every ``w:r`` run in document order paired with its hyperlink target.

    ``target`` is the resolved URL/anchor when the run is inside a
    ``<w:hyperlink>`` (D3), else ``None``. The traversal is preorder DFS -- a run
    is yielded before its own subtree -- which visits exactly the runs, in exactly
    the order, that ``paragraph.iter(w:r)`` did. On a paragraph with no hyperlink
    the target is ``None`` for every run, so the caller's behaviour is unchanged.
    """
    def walk(node: ET.Element, href: str | None) -> Iterable[tuple[ET.Element, str | None]]:
        for child in list(node):
            if child.tag == f"{WORD_NS}hyperlink":
                child_href = _hyperlink_target(child, rels) or href
                yield from walk(child, child_href)
            else:
                if child.tag == f"{WORD_NS}r":
                    yield child, href
                yield from walk(child, href)

    yield from walk(paragraph, None)


def _hyperlink_target(hyperlink: ET.Element, rels: Dict[str, str]) -> str | None:
    """Resolve a ``<w:hyperlink>``'s destination to a safe href, or ``None``.

    An EXTERNAL hyperlink carries an ``r:id`` that keys into the part's
    relationships (``word/_rels/document.xml.rels``); an INTERNAL one carries a
    ``w:anchor`` naming a bookmark in the same document. Only web-safe schemes
    (http/https/mailto/tel), same-document anchors and relative paths are
    returned -- a ``javascript:``/``data:`` target is dropped so a malicious
    upload cannot inject an active-scheme link into the rendered review."""
    rid = hyperlink.get(f"{R_NS_BRACED}id")
    if rid:
        target = rels.get(rid)
        if target:
            return _safe_hyperlink_href(target)
    anchor = _attr(hyperlink, "anchor").strip()
    if anchor:
        return f"#{anchor}"
    return None


def _safe_hyperlink_href(target: str) -> str | None:
    """Return ``target`` if it is a safe href to render, else ``None``.

    Allows http/https/mailto/tel, same-document anchors (``#...``) and relative
    paths; rejects any other explicit URL scheme (``javascript:``, ``data:``,
    ``vbscript:``, ...) so an uploaded DOCX cannot smuggle an active-scheme link."""
    value = str(target or "").strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "tel:", "#", "/", "./", "../")):
        return value
    # Any leading ``scheme:`` that is not whitelisted above is unsafe.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", value):
        return None
    return value


def _read_hyperlink_relationships(document: ZipFile, rels_part_name: str) -> Dict[str, str]:
    """Map relationship id -> target URL for the hyperlink relationships of a part.

    Reads the OPC ``.rels`` sidecar (e.g. ``word/_rels/document.xml.rels``) and
    keeps only ``.../hyperlink`` relationships so a ``w:hyperlink r:id`` can be
    resolved to its destination (D3). A missing/unreadable ``.rels`` degrades to
    an empty map (no hyperlink targets), never an error."""
    try:
        root = _read_xml_part(document, rels_part_name)
    except DocxExtractionError:
        return {}
    rels: Dict[str, str] = {}
    for relationship in root.findall(f"{REL_NS_BRACED}Relationship"):
        rel_id = str(relationship.get("Id") or "").strip()
        target = str(relationship.get("Target") or "").strip()
        rel_type = str(relationship.get("Type") or "")
        if rel_id and target and rel_type.endswith("/hyperlink"):
            rels[rel_id] = target
    return rels


def _paragraph_direction(ppr: ET.Element | None) -> str | None:
    """The paragraph's reading direction (``"rtl"``), or ``None`` for the LTR default.

    Reads the ``<w:pPr>/<w:bidi>`` toggle (D4). Returned only when RTL, so the
    field stays additive and absent on every ordinary left-to-right paragraph."""
    if ppr is None:
        return None
    return "rtl" if _toggle_property(ppr, "bidi") else None


def _run_formatting(run: ET.Element) -> Dict[str, object]:
    rpr = run.find(f"{WORD_NS}rPr")
    formatting: Dict[str, object] = {
        "bold": _toggle_property(rpr, "b"),
        "italic": _toggle_property(rpr, "i"),
        "underline": _underline_property(rpr),
    }
    # ``font`` is additive: present only when the run names one, so a run with no
    # explicit font keeps its prior dict shape. It still drives the merge boundary
    # (see ``_run_formatting_matches``) so differently-fonted runs do not coalesce.
    font = _run_font(rpr)
    if font:
        formatting["font"] = font
    # ``size`` (integer points) is likewise additive: present only when the run
    # carries an explicit ``<w:sz>``. It also drives the merge boundary so runs
    # at different point sizes do not coalesce into one record.
    size = _run_size(rpr)
    if size is not None:
        formatting["size"] = size
    # ``color`` ("#rrggbb"), ``highlight`` (Word color-name string), ``strike``
    # (True) and ``vertAlign`` ("superscript"/"subscript") are all additive: each
    # is present only when the run carries it, and each drives the merge boundary
    # so runs differing on any of them stay distinct records.
    color = _run_color(rpr)
    if color:
        formatting["color"] = color
    highlight = _run_highlight(rpr)
    if highlight:
        formatting["highlight"] = highlight
    if _toggle_property(rpr, "strike"):
        formatting["strike"] = True
    vert_align = _run_vert_align(rpr)
    if vert_align:
        formatting["vertAlign"] = vert_align
    return formatting


def _run_formatting_matches(existing: Dict[str, object], formatting: Dict[str, object]) -> bool:
    if not all(bool(existing.get(key)) == bool(formatting[key]) for key in ("bold", "italic", "underline")):
        return False
    # Runs with different fonts must not merge -- a font change is itself a
    # formatting boundary (sets up the later inline-format milestone).
    if str(existing.get("font") or "") != str(formatting.get("font") or ""):
        return False
    # A point-size change is likewise its own boundary so differently-sized runs
    # stay distinct records.
    if existing.get("size") != formatting.get("size"):
        return False
    # Color/highlight/vertAlign changes are each their own boundary so runs that
    # differ on any of them do not coalesce.
    if str(existing.get("color") or "") != str(formatting.get("color") or ""):
        return False
    if str(existing.get("highlight") or "") != str(formatting.get("highlight") or ""):
        return False
    if str(existing.get("vertAlign") or "") != str(formatting.get("vertAlign") or ""):
        return False
    # A hyperlink boundary: runs with different targets (or link vs no-link) must
    # not coalesce, so each anchor wraps exactly its own text.
    if str(existing.get("hyperlink") or "") != str(formatting.get("hyperlink") or ""):
        return False
    # Strikethrough is a toggle boundary, mirroring bold/italic/underline.
    return bool(existing.get("strike")) == bool(formatting.get("strike"))


def _run_font(rpr: ET.Element | None) -> str:
    if rpr is None:
        return ""
    rfonts = rpr.find(f"{WORD_NS}rFonts")
    if rfonts is None:
        return ""
    return _attr(rfonts, "ascii").strip()


def _run_size(rpr: ET.Element | None) -> int | None:
    """The run's explicit font size in whole points, or ``None`` when unset.

    Word stores sizes in half-points (``<w:sz w:val>``), so points are
    ``round(val / 2)``. ``<w:szCs>`` (complex-script size) is read as a fallback.
    Returned only when present, so the field stays purely additive."""
    if rpr is None:
        return None
    for local_name in ("sz", "szCs"):
        half_points = _int_or_none(_val(rpr.find(f"{WORD_NS}{local_name}")))
        if half_points is not None and half_points > 0:
            return int(round(half_points / 2))
    return None


def _run_color(rpr: ET.Element | None) -> str:
    """The run's explicit text color as ``"#rrggbb"``, or ``""`` when unset.

    Reads ``<w:rPr>/<w:color w:val="RRGGBB"/>``. Word's ``auto`` (theme/automatic
    color) carries no concrete RGB, so it is skipped. Returned lower-cased and
    ``#``-prefixed; the field is present only when a concrete color is named."""
    if rpr is None:
        return ""
    value = _val(rpr.find(f"{WORD_NS}color")).strip()
    if not value or value.lower() == "auto":
        return ""
    if re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return f"#{value.lower()}"
    return ""


def _run_highlight(rpr: ET.Element | None) -> str:
    """The run's highlight color name (e.g. ``"yellow"``), or ``""`` when unset.

    Reads ``<w:rPr>/<w:highlight w:val="yellow"/>``; the value is one of Word's
    named highlight colors, kept as the raw string. ``none`` is treated as unset."""
    if rpr is None:
        return ""
    value = _val(rpr.find(f"{WORD_NS}highlight")).strip()
    if not value or value.lower() == "none":
        return ""
    return value


def _run_vert_align(rpr: ET.Element | None) -> str:
    """The run's vertical alignment (``"superscript"``/``"subscript"``) or ``""``.

    Reads ``<w:rPr>/<w:vertAlign w:val="superscript"/>``. ``baseline`` (the
    default) is treated as unset so the field stays additive."""
    if rpr is None:
        return ""
    value = _val(rpr.find(f"{WORD_NS}vertAlign")).strip().lower()
    if value in {"superscript", "subscript"}:
        return value
    return ""


def _toggle_property(rpr: ET.Element | None, local_name: str) -> bool:
    if rpr is None:
        return False
    element = rpr.find(f"{WORD_NS}{local_name}")
    if element is None:
        return False
    value = _attr(element, "val").strip().lower()
    # An on/off toggle is "on" unless explicitly disabled (val of 0/false/off/none).
    return value not in {"0", "false", "off", "none"}


def _underline_property(rpr: ET.Element | None) -> bool:
    if rpr is None:
        return False
    element = rpr.find(f"{WORD_NS}u")
    if element is None:
        return False
    value = _attr(element, "val").strip().lower()
    return bool(value) and value != "none"


def _trim_run_edges(runs: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Strip the leading/trailing whitespace the flat ``text`` already dropped."""
    trimmed = [dict(run) for run in runs]
    if trimmed:
        trimmed[0]["text"] = str(trimmed[0]["text"]).lstrip()
        trimmed[-1]["text"] = str(trimmed[-1]["text"]).rstrip()
    return [run for run in trimmed if str(run["text"])]


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
