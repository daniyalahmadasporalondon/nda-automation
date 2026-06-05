from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .matter_repository import DiskMatterRepository, MatterRepository
from .pdf_text import PdfExtractionError, extract_pdf_document
from .review_state import CLAUSE_DECISION_FAIL, CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW
from .review_staleness import review_result_staleness, stale_review_message

ANNOTATED_PDF_MIME = "application/pdf"
ANNOTATED_PDF_VERIFICATION_HEADER = "pdf-annotations; evidence-highlights"
MAX_ANNOTATED_CLAUSES = 12
MAX_EVIDENCE_PER_CLAUSE = 4
MAX_SEARCH_TEXT_CHARS = 220
MAX_NOTE_CHARS = 900


@dataclass(frozen=True)
class AnnotatedPdfExport:
    data: bytes
    filename: str
    annotation_count: int
    unmatched_evidence_count: int


class AnnotatedPdfExportError(ValueError):
    pass


class AnnotatedPdfDependencyError(AnnotatedPdfExportError):
    pass


class AnnotatedPdfMatterNotFoundError(AnnotatedPdfExportError):
    pass


class AnnotatedPdfUnsupportedSourceError(AnnotatedPdfExportError):
    pass


class StaleAnnotatedPdfReviewError(AnnotatedPdfExportError):
    def __init__(self, summary: dict):
        reasons = summary.get("stale_reasons")
        self.reasons = [str(reason) for reason in reasons] if isinstance(reasons, list) else []
        self.summary = summary
        super().__init__(stale_review_message(self.reasons))


def build_matter_annotated_pdf(
    matter_id: str,
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
) -> AnnotatedPdfExport:
    repository = repository or DiskMatterRepository()
    matter = repository.get_matter(str(matter_id or "").strip(), owner_user_id=owner_user_id)
    if matter is None:
        raise AnnotatedPdfMatterNotFoundError("Matter not found.")

    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        raise AnnotatedPdfExportError("Matter does not have a stored review result.")

    staleness = review_result_staleness(review_result)
    if staleness["stale"]:
        raise StaleAnnotatedPdfReviewError(staleness)

    source_filename = str(matter.get("source_filename") or "")
    if not source_filename.lower().endswith(".pdf"):
        raise AnnotatedPdfUnsupportedSourceError("Annotated PDF export is available only for PDF matters.")

    source_bytes = repository.get_source_document_bytes(matter)
    if source_bytes is None:
        raise AnnotatedPdfExportError("Matter source PDF is missing from storage.")

    return build_annotated_pdf(source_bytes, source_filename=source_filename, review_result=review_result)


def build_annotated_pdf(source_pdf: bytes, *, source_filename: str, review_result: Mapping[str, Any]) -> AnnotatedPdfExport:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - exercised when optional pdf extra is absent.
        raise AnnotatedPdfDependencyError("Annotated PDF export requires PyMuPDF/fitz.") from exc

    try:
        document = fitz.open(stream=source_pdf, filetype="pdf")
    except Exception as exc:
        raise AnnotatedPdfExportError("The source PDF could not be opened for annotation.") from exc

    try:
        extraction = extract_pdf_document(source_pdf)
    except PdfExtractionError as exc:
        raise AnnotatedPdfExportError(str(exc)) from exc

    paragraphs_by_id = {
        str(paragraph.get("id") or ""): paragraph
        for paragraph in extraction.paragraphs
        if str(paragraph.get("id") or "")
    }
    annotation_count = 0
    unmatched_count = 0
    seen_matches: set[tuple[str, int, str]] = set()
    for clause in _annotation_clauses(review_result):
        color = _clause_color(clause)
        note_text = _clause_note_text(clause)
        evidence_items = _clause_evidence(clause)
        clause_matched = False
        for evidence in evidence_items[:MAX_EVIDENCE_PER_CLAUSE]:
            paragraph = paragraphs_by_id.get(str(evidence.get("paragraph_id") or ""))
            page_number = _page_number(paragraph) or _page_number(evidence)
            if not page_number:
                unmatched_count += 1
                continue
            page_index = page_number - 1
            if page_index < 0 or page_index >= document.page_count:
                unmatched_count += 1
                continue
            page = document[page_index]
            search_texts = _search_text_candidates(evidence, paragraph)
            match_rects = []
            match_key_text = ""
            for search_text in search_texts:
                match_rects = page.search_for(search_text, quads=False)
                if match_rects:
                    match_key_text = search_text
                    break
            if not match_rects:
                unmatched_count += 1
                continue
            match_key = (str(clause.get("id") or ""), page_index, _normalize_text(match_key_text)[:160])
            if match_key in seen_matches:
                continue
            seen_matches.add(match_key)
            highlight = page.add_highlight_annot(match_rects)
            highlight.set_colors(stroke=color)
            highlight.set_info(title=_clause_title(clause), content=note_text)
            highlight.update()
            if not clause_matched:
                note_point = _note_point(page, match_rects[0])
                note = page.add_text_annot(note_point, note_text)
                note.set_info(title=_clause_title(clause), content=note_text)
                note.update()
                clause_matched = True
            annotation_count += 1

    output = document.write(garbage=4, deflate=True)
    document.close()
    return AnnotatedPdfExport(
        data=bytes(output),
        filename=annotated_pdf_download_filename(source_filename),
        annotation_count=annotation_count,
        unmatched_evidence_count=unmatched_count,
    )


def annotated_pdf_download_filename(filename: str) -> str:
    source_name = Path(filename).stem if filename else ""
    safe_name = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in source_name)
    safe_name = re.sub(r"-{2,}", "-", safe_name)
    safe_name = safe_name.strip("-_") or "nda"
    return f"{safe_name}-annotated-review.pdf"


def _annotation_clauses(review_result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    clauses = review_result.get("clauses")
    if not isinstance(clauses, list):
        return []
    eligible = [clause for clause in clauses if isinstance(clause, Mapping) and _clause_evidence(clause)]
    eligible.sort(key=_clause_priority)
    return eligible[:MAX_ANNOTATED_CLAUSES]


def _clause_priority(clause: Mapping[str, Any]) -> tuple[int, str]:
    decision = str(clause.get("decision") or "").lower()
    if decision == CLAUSE_DECISION_FAIL:
        priority = 0
    elif decision == CLAUSE_DECISION_REVIEW or clause.get("needs_review") is True:
        priority = 1
    else:
        priority = 2
    return priority, str(clause.get("name") or clause.get("id") or "")


def _clause_evidence(clause: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    structured = clause.get("structured_evidence")
    if isinstance(structured, list) and structured:
        return [item for item in structured if isinstance(item, Mapping)]
    paragraphs = clause.get("evidence_paragraphs")
    if isinstance(paragraphs, list):
        return [item for item in paragraphs if isinstance(item, Mapping)]
    return []


def _search_text_candidates(evidence: Mapping[str, Any], paragraph: Mapping[str, Any] | None) -> list[str]:
    candidates = [
        evidence.get("matched_text"),
        evidence.get("text"),
        paragraph.get("text") if isinstance(paragraph, Mapping) else "",
    ]
    spans = evidence.get("match_spans")
    if isinstance(spans, list):
        for span in spans:
            if isinstance(span, Mapping):
                candidates.insert(0, span.get("text"))

    output: list[str] = []
    for candidate in candidates:
        text = _searchable_text(candidate)
        for variant in _search_variants(text):
            if variant and variant not in output:
                output.append(variant)
    return output


def _searchable_text(value: object) -> str:
    text = _normalize_text(value)
    if len(text) <= MAX_SEARCH_TEXT_CHARS:
        return text
    sentences = re.split(r"(?<=[.;:])\s+", text)
    for sentence in sentences:
        if 35 <= len(sentence) <= MAX_SEARCH_TEXT_CHARS:
            return sentence
    return text[:MAX_SEARCH_TEXT_CHARS].rsplit(" ", 1)[0]


def _search_variants(text: str) -> list[str]:
    if not text:
        return []
    variants = [
        text,
        text.replace("“", '"').replace("”", '"').replace("’", "'"),
        text.replace('"', "“", 1).replace('"', "”", 1),
    ]
    words = text.split()
    if len(words) > 10:
        variants.append(" ".join(words[:18]))
        variants.append(" ".join(words[-18:]))
    return [_normalize_text(variant) for variant in variants if _normalize_text(variant)]


def _clause_color(clause: Mapping[str, Any]) -> tuple[float, float, float]:
    decision = str(clause.get("decision") or "").lower()
    if decision == CLAUSE_DECISION_FAIL:
        return 1.0, 0.35, 0.35
    if decision == CLAUSE_DECISION_REVIEW or clause.get("needs_review") is True:
        return 1.0, 0.78, 0.22
    if decision == CLAUSE_DECISION_PASS:
        return 0.25, 0.78, 0.50
    return 0.60, 0.50, 0.85


def _clause_note_text(clause: Mapping[str, Any]) -> str:
    parts = [
        f"{_clause_title(clause)}: {_decision_label(clause)}",
        str(clause.get("decision_reason") or clause.get("reason") or clause.get("finding") or "").strip(),
    ]
    proposed_redline = clause.get("proposed_redline")
    if isinstance(proposed_redline, Mapping):
        redline_text = str(proposed_redline.get("text") or "").strip()
        if redline_text:
            parts.append(f"Proposed change: {redline_text}")
    return "\n\n".join(part for part in parts if part)[:MAX_NOTE_CHARS]


def _clause_title(clause: Mapping[str, Any]) -> str:
    return str(clause.get("name") or clause.get("id") or "Clause").strip()


def _decision_label(clause: Mapping[str, Any]) -> str:
    decision = str(clause.get("decision") or "").strip().lower()
    if decision == CLAUSE_DECISION_FAIL:
        return "Fail"
    if decision == CLAUSE_DECISION_REVIEW or clause.get("needs_review") is True:
        return "Needs review"
    if decision == CLAUSE_DECISION_PASS:
        return "Pass"
    return "Reviewed"


def _page_number(value: Mapping[str, Any] | None) -> int | None:
    if not isinstance(value, Mapping):
        return None
    try:
        page_number = int(value.get("page_number") or 0)
    except (TypeError, ValueError):
        return None
    return page_number if page_number > 0 else None


def _note_point(page: Any, rect: Any) -> Any:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise AnnotatedPdfDependencyError("Annotated PDF export requires PyMuPDF/fitz.") from exc

    x = min(rect.x1 + 12, page.rect.width - 24)
    y = max(rect.y0, 24)
    return fitz.Point(x, y)


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())
