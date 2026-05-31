from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from .review_document import Paragraph


class PdfExtractionError(ValueError):
    """Raised when a PDF file cannot be converted into reviewable text."""


@dataclass(frozen=True)
class PdfExtraction:
    paragraphs: List[Paragraph]
    quality: dict[str, object]


def extract_pdf_text(data: bytes) -> str:
    paragraphs = extract_pdf_paragraphs(data)
    return "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)


def extract_pdf_paragraphs(data: bytes) -> List[Paragraph]:
    return extract_pdf_document(data).paragraphs


def extract_pdf_document(data: bytes) -> PdfExtraction:
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise PdfExtractionError("The uploaded file is not a valid PDF document.") from exc

    page_lines: list[list[str]] = []
    page_texts: list[str] = []
    page_count = len(reader.pages)
    pages_without_text = 0
    pages_with_text = 0
    repeated_margins: set[str] = set()
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            raise PdfExtractionError("The PDF text could not be extracted.") from exc
        normalized_lines = _normalized_lines(page_text)
        page_lines.append(normalized_lines)
        page_texts.append("\n".join(normalized_lines))
        if normalized_lines:
            pages_with_text += 1
        else:
            pages_without_text += 1

    if page_count > 1:
        repeated_margins = _repeated_margin_lines(page_lines)

    paragraphs: List[Paragraph] = []
    for page_index, lines in enumerate(page_lines, start=1):
        filtered_lines = [line for line in lines if line not in repeated_margins and not _is_page_number(line)]
        for paragraph_text in _split_pdf_paragraphs(filtered_lines):
            paragraphs.append({
                "id": f"p{len(paragraphs) + 1}",
                "source_index": len(paragraphs) + 1,
                "source_part": "pdf",
                "page_number": page_index,
                "text": paragraph_text,
            })

    if not paragraphs:
        raise PdfExtractionError("No readable text was found in the PDF. Scanned PDFs need OCR before review.")
    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in paragraphs)
    return PdfExtraction(
        paragraphs=paragraphs,
        quality=_pdf_quality_report(
            page_count=page_count,
            pages_with_text=pages_with_text,
            pages_without_text=pages_without_text,
            extracted_text=extracted_text,
            paragraph_count=len(paragraphs),
            repeated_margin_count=len(repeated_margins),
        ),
    )


def _normalized_lines(text: str) -> list[str]:
    return [
        " ".join(raw_line.split())
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if " ".join(raw_line.split())
    ]


def _split_pdf_paragraphs(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if _starts_new_pdf_paragraph(line, current):
            blocks.append(" ".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append(" ".join(current))
    return blocks


def _starts_new_pdf_paragraph(line: str, current: list[str]) -> bool:
    if not current:
        return False
    if _is_standalone_clause_number(current[-1]):
        return False
    if _is_heading(line):
        return True
    if _is_clause_start(line):
        return True
    previous = current[-1]
    if _ends_sentence(previous) and _looks_like_sentence_start(line):
        return True
    return False


def _is_clause_start(line: str) -> bool:
    return bool(re.match(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.|\([a-z0-9ivx]+\))\s+\S", line))


def _is_standalone_clause_number(line: str) -> bool:
    return bool(re.match(r"^\d+(?:\.\d+)*\.?$", line))


def _is_heading(line: str) -> bool:
    words = re.findall(r"[A-Za-z]+", line)
    if len(words) < 3 or len(words) > 14 or len(line) > 120:
        return False
    if words[0].lower() in {"this", "the", "each", "either", "any", "such"}:
        return False
    uppercase_words = [word for word in words if word.isupper() or word[:1].isupper()]
    return len(uppercase_words) >= max(1, int(len(words) * 0.75)) and not _ends_sentence(line)


def _ends_sentence(line: str) -> bool:
    return bool(re.search(r"[.;:!?)](?:[\"”’])?$", line))


def _looks_like_sentence_start(line: str) -> bool:
    return bool(re.match(r"^(?:[A-Z0-9(]|“|\"|')", line))


def _is_page_number(line: str) -> bool:
    return bool(re.match(r"^(?:page\s+)?\d+(?:\s+of\s+\d+)?$", line, flags=re.IGNORECASE))


def _repeated_margin_lines(page_lines: list[list[str]]) -> set[str]:
    candidates: dict[str, int] = {}
    for lines in page_lines:
        for line in set([*lines[:2], *lines[-2:]]):
            if len(line) < 4 or _is_page_number(line):
                continue
            candidates[line] = candidates.get(line, 0) + 1
    minimum_repeats = max(2, int(len(page_lines) * 0.5))
    return {line for line, count in candidates.items() if count >= minimum_repeats}


def _pdf_quality_report(
    *,
    page_count: int,
    pages_with_text: int,
    pages_without_text: int,
    extracted_text: str,
    paragraph_count: int,
    repeated_margin_count: int,
) -> dict[str, object]:
    extracted_characters = len(extracted_text)
    warnings: list[dict[str, object]] = []
    if pages_without_text:
        warnings.append({
            "type": "pdf_pages_without_text",
            "message": f"{pages_without_text} PDF page(s) produced no extractable text.",
        })
    if extracted_characters < 500:
        warnings.append({
            "type": "pdf_sparse_text",
            "message": "The PDF produced a small amount of extractable text; review the source carefully.",
        })
    if paragraph_count <= max(1, page_count // 2) and extracted_characters > 1000:
        warnings.append({
            "type": "pdf_low_paragraph_count",
            "message": "The PDF text may have been extracted as overly large paragraphs.",
        })
    if _garbled_text_ratio(extracted_text) > 0.25:
        warnings.append({
            "type": "pdf_garbled_text",
            "message": "The PDF extraction contains an unusual amount of symbols or spacing.",
        })
    return {
        "page_count": page_count,
        "pages_with_text": pages_with_text,
        "pages_without_text": pages_without_text,
        "extracted_characters": extracted_characters,
        "extracted_paragraphs": paragraph_count,
        "repeated_margin_lines_removed": repeated_margin_count,
        "warnings": warnings,
    }


def _garbled_text_ratio(text: str) -> float:
    if not text:
        return 1.0
    suspicious = len(re.findall(r"[^A-Za-z0-9\s.,;:!?()\\[\\]{}'\"“”‘’/@&%$#*+\\-–—]", text))
    return suspicious / max(1, len(text))
