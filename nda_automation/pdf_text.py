from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List

from .review_document import Paragraph


class PdfExtractionError(ValueError):
    """Raised when a PDF file cannot be converted into reviewable text."""


INVALID_PDF_MESSAGE = "The uploaded file is not a valid PDF document."
ENCRYPTED_PDF_MESSAGE = "The PDF is encrypted or password-protected. Remove the password before reviewing."
PDF_SUPPORT_NOT_INSTALLED_MESSAGE = "PDF support is not installed. Install the pypdf dependency before reviewing PDF files."
MAX_PDF_PAGES = 100
MAX_PDF_EXTRACTED_CHARACTERS = 500_000


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
    except ImportError as exc:
        raise PdfExtractionError(PDF_SUPPORT_NOT_INSTALLED_MESSAGE) from exc

    if not data.lstrip().startswith(b"%PDF-"):
        raise PdfExtractionError(INVALID_PDF_MESSAGE)

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise PdfExtractionError(INVALID_PDF_MESSAGE) from exc

    if reader.is_encrypted:
        # Empty-password-encrypted PDFs decrypt and remain reviewable; truly
        # locked PDFs must be reported as encrypted rather than "scanned/invalid".
        try:
            decrypted = reader.decrypt("")
        except Exception as exc:
            raise PdfExtractionError(ENCRYPTED_PDF_MESSAGE) from exc
        if not decrypted:
            raise PdfExtractionError(ENCRYPTED_PDF_MESSAGE)

    page_lines: list[list[str]] = []
    page_count = len(reader.pages)
    if page_count > MAX_PDF_PAGES:
        raise PdfExtractionError(f"The PDF has {page_count} pages, which exceeds the {MAX_PDF_PAGES} page review limit.")
    pages_without_text = 0
    pages_with_text = 0
    extracted_character_count = 0
    repeated_margins: set[str] = set()
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            raise PdfExtractionError("The PDF text could not be extracted.") from exc
        extracted_character_count += len(page_text)
        if extracted_character_count > MAX_PDF_EXTRACTED_CHARACTERS:
            raise PdfExtractionError(
                f"The PDF produced more than the {MAX_PDF_EXTRACTED_CHARACTERS:,} character extraction limit."
            )
        normalized_lines = _normalized_lines(page_text)
        page_lines.append(normalized_lines)
        if normalized_lines:
            pages_with_text += 1
        else:
            pages_without_text += 1

    if page_count > 1:
        repeated_margins = _repeated_margin_lines(page_lines)

    paragraphs: List[Paragraph] = []
    for page_index, lines in enumerate(page_lines, start=1):
        filtered_lines = _filtered_pdf_lines(lines, repeated_margins)
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
    visual_profile = _pdf_visual_profile(data)
    return PdfExtraction(
        paragraphs=paragraphs,
        quality=_pdf_quality_report(
            page_count=page_count,
            pages_with_text=pages_with_text,
            pages_without_text=pages_without_text,
            extracted_text=extracted_text,
            paragraph_count=len(paragraphs),
            repeated_margin_count=len(repeated_margins),
            visual_profile=visual_profile,
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


def _filtered_pdf_lines(lines: list[str], repeated_margins: set[str]) -> list[str]:
    filtered: list[str] = []
    for index, line in enumerate(lines):
        if line in repeated_margins:
            continue
        if _is_disposable_page_number(line, index, lines):
            continue
        filtered.append(line)
    return filtered


def _is_disposable_page_number(line: str, index: int, lines: list[str]) -> bool:
    if not _is_page_number(line):
        return False
    if _looks_like_standalone_clause_marker(line, index, lines):
        return False
    return True


def _looks_like_standalone_clause_marker(line: str, index: int, lines: list[str]) -> bool:
    if not _is_standalone_clause_number(line):
        return False
    if index >= len(lines) - 1:
        return False
    next_line = lines[index + 1]
    if _is_page_number(next_line):
        return False
    return bool(re.search(r"[A-Za-z]", next_line)) and len(next_line) <= 120


def _repeated_margin_lines(page_lines: list[list[str]]) -> set[str]:
    candidates: dict[str, int] = {}
    for lines in page_lines:
        for line in set([*lines[:2], *lines[-2:]]):
            if len(line) < 4 or _is_page_number(line) or not _is_non_substantive_margin_line(line):
                continue
            candidates[line] = candidates.get(line, 0) + 1
    minimum_repeats = max(2, int(len(page_lines) * 0.5))
    return {line for line, count in candidates.items() if count >= minimum_repeats}


def _is_non_substantive_margin_line(line: str) -> bool:
    normalized = str(line or "").strip()
    if not normalized or len(normalized) > 80 or _ends_sentence(normalized):
        return False
    words = re.findall(r"[A-Za-z]+", normalized)
    if len(words) > 2:
        return False
    return not re.search(
        r"\b(?:"
        r"agreement|clause|confidential|confidentiality|definition|disclos(?:e|ing|ure)|"
        r"information|mutual|nda|non-disclosure|nondisclosure|obligation|party|parties|"
        r"recipient|schedule|term|undertaking"
        r")\b",
        normalized,
        flags=re.IGNORECASE,
    )


def _pdf_quality_report(
    *,
    page_count: int,
    pages_with_text: int,
    pages_without_text: int,
    extracted_text: str,
    paragraph_count: int,
    repeated_margin_count: int,
    visual_profile: dict[str, object] | None = None,
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
    if _visual_profile_requires_source_preview(visual_profile):
        warnings.append({
            "type": "pdf_visual_fidelity_requires_source_preview",
            "message": (
                "The PDF contains visual layout, color, image, or line-art signals that plain text extraction "
                "cannot preserve. Use the original PDF/page preview for layout review."
            ),
        })
    quality: dict[str, object] = {
        "page_count": page_count,
        "pages_with_text": pages_with_text,
        "pages_without_text": pages_without_text,
        "extracted_characters": extracted_characters,
        "extracted_paragraphs": paragraph_count,
        "repeated_margin_lines_removed": repeated_margin_count,
        "warnings": warnings,
    }
    if visual_profile:
        quality["visual_profile"] = visual_profile
    return quality


def _pdf_visual_profile(data: bytes) -> dict[str, object]:
    """Return best-effort PDF visual signals that text extraction cannot preserve."""

    try:
        import fitz
    except ImportError:
        return {
            "status": "unavailable",
            "reason": "pymupdf_not_installed",
            "requires_source_preview": True,
        }

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return {
            "status": "unavailable",
            "reason": "visual_profile_failed",
            "requires_source_preview": True,
        }

    try:
        text_span_count = 0
        non_black_text_span_count = 0
        image_count = 0
        drawing_count = 0
        pages_with_non_black_text = 0
        pages_with_images = 0
        pages_with_drawings = 0
        unique_text_colors: set[int] = set()
        page_count = document.page_count
        profiled_pages = min(page_count, MAX_PDF_PAGES)
        for page_index in range(profiled_pages):
            page = document[page_index]
            page_has_non_black_text = False
            page_has_images = False
            page_has_drawings = False
            try:
                blocks = page.get_text("dict").get("blocks", [])
            except Exception:
                blocks = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == 1:
                    image_count += 1
                    page_has_images = True
                    continue
                if block.get("type") != 0:
                    continue
                for line in block.get("lines") or []:
                    if not isinstance(line, dict):
                        continue
                    for span in line.get("spans") or []:
                        if not isinstance(span, dict):
                            continue
                        text_span_count += 1
                        color = _safe_int(span.get("color"))
                        if color is None:
                            continue
                        unique_text_colors.add(color)
                        if color != 0:
                            non_black_text_span_count += 1
                            page_has_non_black_text = True
            try:
                page_drawings = page.get_drawings()
            except Exception:
                page_drawings = []
            if page_drawings:
                drawing_count += len(page_drawings)
                page_has_drawings = True
            if page_has_non_black_text:
                pages_with_non_black_text += 1
            if page_has_images:
                pages_with_images += 1
            if page_has_drawings:
                pages_with_drawings += 1
    except Exception:
        return {
            "status": "unavailable",
            "reason": "visual_profile_failed",
            "requires_source_preview": True,
        }
    finally:
        document.close()

    visual_features: list[str] = []
    if non_black_text_span_count:
        visual_features.append("colored_text")
    if drawing_count:
        visual_features.append("drawings_or_borders")
    if image_count:
        visual_features.append("images")
    requires_source_preview = bool(visual_features)
    return {
        "status": "ready",
        "page_count": page_count,
        "profiled_pages": profiled_pages,
        "text_span_count": text_span_count,
        "non_black_text_span_count": non_black_text_span_count,
        "unique_text_color_count": len(unique_text_colors),
        "drawing_count": drawing_count,
        "image_count": image_count,
        "pages_with_non_black_text": pages_with_non_black_text,
        "pages_with_drawings": pages_with_drawings,
        "pages_with_images": pages_with_images,
        "visual_features": visual_features,
        "requires_source_preview": requires_source_preview,
    }


def _visual_profile_requires_source_preview(visual_profile: dict[str, object] | None) -> bool:
    if not isinstance(visual_profile, dict):
        return False
    return bool(visual_profile.get("requires_source_preview"))


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _garbled_text_ratio(text: str) -> float:
    if not text:
        return 1.0
    allowed_punctuation = set(".,;:!?()[]{}'\"“”‘’/@&%$#*+-–—\\")
    suspicious = sum(
        1
        for character in text
        if not character.isalnum() and not character.isspace() and character not in allowed_punctuation
    )
    return suspicious / max(1, len(text))
