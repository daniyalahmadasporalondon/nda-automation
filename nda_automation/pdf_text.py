from __future__ import annotations

from typing import List

from .review_document import Paragraph


class PdfExtractionError(ValueError):
    """Raised when a PDF file cannot be converted into reviewable text."""


def extract_pdf_text(data: bytes) -> str:
    paragraphs = extract_pdf_paragraphs(data)
    return "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)


def extract_pdf_paragraphs(data: bytes) -> List[Paragraph]:
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise PdfExtractionError("The uploaded file is not a valid PDF document.") from exc

    paragraphs: List[Paragraph] = []
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            raise PdfExtractionError("The PDF text could not be extracted.") from exc
        for paragraph_text in _split_pdf_paragraphs(page_text):
            paragraphs.append({
                "id": f"p{len(paragraphs) + 1}",
                "source_index": len(paragraphs) + 1,
                "source_part": "pdf",
                "page_number": page_index,
                "text": paragraph_text,
            })

    if not paragraphs:
        raise PdfExtractionError("No readable text was found in the PDF. Scanned PDFs need OCR before review.")
    return paragraphs


def _split_pdf_paragraphs(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw_line.split())
        if not line:
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(" ".join(current))
    return blocks
