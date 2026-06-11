from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .matter_repository import DiskMatterRepository, MatterRepository
from .pdf_text import PdfExtractionError, extract_pdf_document
from .review_state import CLAUSE_DECISION_FAIL, CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW
from .review_staleness import review_result_staleness, stale_review_message

ANNOTATED_PDF_MIME = "application/pdf"
ANNOTATED_PDF_VERIFICATION_HEADER = "pdf-annotations; evidence-highlights; proposed-change-markup"
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
        extraction = extract_pdf_document(source_pdf)
    except PdfExtractionError as exc:
        raise AnnotatedPdfExportError(str(exc)) from exc

    paragraphs_by_id = {
        str(paragraph.get("id") or ""): paragraph
        for paragraph in extraction.paragraphs
        if str(paragraph.get("id") or "")
    }
    with tempfile.TemporaryDirectory(prefix="nda-annotated-pdf-") as temp_dir:
        source_path = Path(temp_dir) / "source.pdf"
        source_path.write_bytes(source_pdf)
        try:
            document = fitz.open(str(source_path))
        except Exception as exc:
            raise AnnotatedPdfExportError("The source PDF could not be opened for annotation.") from exc

        annotation_count = 0
        unmatched_count = 0
        seen_matches: set[tuple[str, int, str]] = set()
        try:
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
                    match_rects, match_key_text = _match_evidence_rects(page, search_texts)
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
                    annotation_count += 1
                    if not clause_matched:
                        note_point = _note_point(page, match_rects[0])
                        note = page.add_text_annot(note_point, note_text)
                        note.set_info(title=_clause_title(clause), content=note_text)
                        note.update()
                        annotation_count += 1
                        annotation_count += _add_visible_proposed_change(page, match_rects, clause)
                        clause_matched = True
            output = _save_incremental_pdf(document, source_path)
        finally:
            _close_pdf_document(document)
    return AnnotatedPdfExport(
        data=output,
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


def _match_evidence_rects(page: Any, search_texts: list[str]) -> tuple[list[Any], str]:
    for search_text in search_texts:
        match_rects = _word_coordinate_rects(page, search_text)
        if match_rects:
            return match_rects, search_text
        match_rects = page.search_for(search_text, quads=False)
        if match_rects:
            return list(match_rects), search_text
    return [], ""


def _word_coordinate_rects(page: Any, search_text: str) -> list[Any]:
    target = _word_match_key(search_text)
    if not target:
        return []
    try:
        words = page.get_text("words")
    except Exception:
        return []
    word_records: list[tuple[str, Any]] = []
    for word in words or []:
        if len(word) < 5:
            continue
        token = _word_match_key(word[4])
        if not token:
            continue
        word_records.append((token, _rect_from_word(word)))

    target_tokens = target.split()
    if not target_tokens:
        return []
    max_tokens = len(target_tokens)
    for start_index in range(len(word_records)):
        current_tokens: list[str] = []
        current_rects: list[Any] = []
        for offset, (token, rect) in enumerate(word_records[start_index : start_index + max_tokens + 3]):
            current_tokens.append(token)
            current_rects.append(rect)
            phrase = " ".join(current_tokens)
            if phrase == target:
                return current_rects
            if offset >= max_tokens + 2 or len(phrase) > len(target) + 20:
                break
    return []


def _rect_from_word(word: Any) -> Any:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise AnnotatedPdfDependencyError("Annotated PDF export requires PyMuPDF/fitz.") from exc

    return fitz.Rect(float(word[0]), float(word[1]), float(word[2]), float(word[3]))


def _word_match_key(value: object) -> str:
    text = _normalize_text(value).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


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
    proposed_change_text = _clause_proposed_change_text(clause)
    if proposed_change_text:
        parts.append(f"Proposed change: {proposed_change_text}")
    return "\n\n".join(part for part in parts if part)[:MAX_NOTE_CHARS]


def _clause_proposed_change_text(clause: Mapping[str, Any]) -> str:
    proposed_change = clause.get("proposed_change")
    if isinstance(proposed_change, Mapping):
        for key in ("proposed_text", "replacement_text", "new_text", "text"):
            text = str(proposed_change.get(key) or "").strip()
            if text:
                return text
    proposed_redline = clause.get("proposed_redline")
    if isinstance(proposed_redline, Mapping):
        return str(proposed_redline.get("text") or "").strip()
    return ""


def _add_visible_proposed_change(page: Any, match_rects: list[Any], clause: Mapping[str, Any]) -> int:
    proposed_text = _clause_proposed_change_text(clause)
    if not proposed_text or not match_rects:
        return 0
    title = f"{_clause_title(clause)} proposed change"
    content = f"Proposed change: {proposed_text}"[:MAX_NOTE_CHARS]
    annotation_count = 0
    strikeout = page.add_strikeout_annot(match_rects)
    strikeout.set_colors(stroke=(0.85, 0.12, 0.12))
    strikeout.set_info(title=title, content=content)
    strikeout.update()
    annotation_count += 1

    free_text = page.add_freetext_annot(
        _proposed_change_rect(page, match_rects[0]),
        content,
        fontsize=8,
        text_color=(0.55, 0.05, 0.05),
        fill_color=(1.0, 0.96, 0.78),
    )
    free_text.set_info(title=title, content=content)
    free_text.update()
    annotation_count += 1
    return annotation_count


def _proposed_change_rect(page: Any, rect: Any) -> Any:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise AnnotatedPdfDependencyError("Annotated PDF export requires PyMuPDF/fitz.") from exc

    width = min(max(220.0, rect.width + 160.0), max(120.0, page.rect.width - 48.0))
    x0 = min(max(24.0, rect.x0), max(24.0, page.rect.width - width - 24.0))
    x1 = min(page.rect.width - 24.0, x0 + width)
    y0 = rect.y1 + 10.0
    y1 = y0 + 52.0
    if y1 > page.rect.height - 24.0:
        y1 = max(76.0, rect.y0 - 10.0)
        y0 = max(24.0, y1 - 52.0)
    return fitz.Rect(x0, y0, x1, y1)


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


def _save_incremental_pdf(document: Any, source_path: Path) -> bytes:
    try:
        document.saveIncr()
        return source_path.read_bytes()
    except Exception:
        return bytes(document.write(garbage=4, deflate=True))


def _close_pdf_document(document: Any) -> None:
    try:
        document.close()
    except Exception:
        pass


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())
