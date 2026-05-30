from __future__ import annotations

from io import BytesIO
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocxExtractionError(ValueError):
    """Raised when a DOCX file cannot be converted into reviewable text."""


def extract_docx_text(data: bytes) -> str:
    try:
        with ZipFile(BytesIO(data)) as document:
            try:
                document_xml = document.read("word/document.xml")
            except KeyError as exc:
                raise DocxExtractionError("The Word document is missing its main document body.") from exc
    except BadZipFile as exc:
        raise DocxExtractionError("The uploaded file is not a valid .docx document.") from exc

    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError as exc:
        raise DocxExtractionError("The Word document body could not be read.") from exc

    paragraphs = []
    for paragraph in root.iter(f"{WORD_NS}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{WORD_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{WORD_NS}tab":
                parts.append("\t")
            elif node.tag in {f"{WORD_NS}br", f"{WORD_NS}cr"}:
                parts.append("\n")

        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)

    extracted = "\n\n".join(paragraphs).strip()
    if not extracted:
        raise DocxExtractionError("No readable text was found in the Word document.")
    return extracted

