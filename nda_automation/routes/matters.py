from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from pathlib import Path

from .. import export_service, gmail_integration, matter_store, matter_view, telemetry
from ..ai_assessor import AIAssessorError, assess_nda_with_ai
from ..checker import (
    EvidenceProvenanceError,
    ParagraphAlignmentError,
    PlaybookTemplateError,
    REVIEW_ENGINE_VERSION,
    review_nda,
)
from ..document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from ..docx_text import DocxExtractionError
from ..http_auth import _env_flag_enabled
from ..ingestion_service import create_matter_from_document, is_supported_document_filename
from ..pdf_text import PdfExtractionError
from ..triage import triage_review_result
from .common import parse_matter_id

AI_FIRST_REVIEW_FEATURE_FLAG = "NDA_AI_FIRST_REVIEW_ENABLED"
HTTP_MATTER_SOURCE_COLUMNS = {"manual_upload": "in_review"}
MATTER_BOARD_COLUMNS = {"gmail_demo", "in_review", "redline_ready", "signed_closed"}
MAX_REDLINE_DRAFT_ITEMS = 200


def handle_matter_list(handler, *, send_body: bool = True) -> None:
    try:
        handler._send_json({"matters": matter_view.public_matters(matter_store.list_matters())}, send_body=send_body)
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)


def handle_matter_review(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/review")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    try:
        matter = matter_store.get_matter(matter_id)
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    matter = refresh_stale_matter_review(matter)
    handler._send_json(matter_view.review_matter(matter), send_body=send_body)


def handle_matter_detail(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path)
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    try:
        matter = matter_store.get_matter(matter_id)
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)}, send_body=send_body)


def refresh_stale_matter_review(matter: dict) -> dict:
    if not review_result_is_stale(matter.get("review_result")):
        return matter
    extracted_text = str(matter.get("extracted_text") or "")
    if not extracted_text.strip():
        return matter
    try:
        review_result = review_nda(extracted_text)
    except (EvidenceProvenanceError, ParagraphAlignmentError, PlaybookTemplateError, ValueError):
        return matter
    updated_matter = matter_store.update_matter_review(
        str(matter.get("id") or ""),
        review_result,
        triage_review_result(review_result),
    )
    return updated_matter or {
        **matter,
        "review_result": review_result,
        **triage_review_result(review_result),
    }


def review_result_is_stale(review_result: object) -> bool:
    if not isinstance(review_result, dict):
        return True
    if review_result.get("review_engine_version") != REVIEW_ENGINE_VERSION:
        return True
    clauses = review_result.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        return True
    if not isinstance(review_result.get("review_state"), dict):
        return True
    return any(
        not isinstance(clause, dict)
        or not isinstance(clause.get("structure_context"), dict)
        or not isinstance(clause.get("review_state"), dict)
        for clause in clauses
    )


def handle_matter_source(handler, path: str, *, send_body: bool = True) -> None:
    """Stream a matter's stored original .docx/.pdf for faithful rendering."""
    matter_id = parse_matter_id(path, suffix="/source")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    try:
        matter = matter_store.get_matter(matter_id)
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    source_path = matter_store.source_document_path(matter)
    if source_path is None:
        handler._send_json({"error": "No source document for this matter."}, status=404, send_body=send_body)
        return
    ext = source_path.suffix.lower()
    if ext == ".docx":
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif ext == ".pdf":
        mime = "application/pdf"
    else:
        mime = "application/octet-stream"
    handler._send_file(source_path, content_type=mime, send_body=send_body)


def handle_matter_upload(handler, *, create_matter_from_document_func=create_matter_from_document) -> None:
    telemetry.increment("matter_upload_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    filename = payload.get("filename", "")
    content_base64 = payload.get("content_base64", "")
    source_type = payload.get("source_type", "manual_upload")
    if not is_supported_document_filename(filename):
        handler._send_json({"error": "Upload a .docx Word document or text-based PDF."}, status=400)
        return
    if not isinstance(content_base64, str) or not content_base64:
        handler._send_json({"error": "Provide a document to import."}, status=400)
        return
    if not isinstance(source_type, str) or not source_type.strip():
        source_type = "manual_upload"
    source_type = source_type.strip()
    board_column = HTTP_MATTER_SOURCE_COLUMNS.get(source_type)
    if board_column is None:
        handler._send_json({"error": "Unsupported matter source."}, status=400)
        return

    try:
        document_bytes = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        handler._send_json({"error": "The uploaded document could not be decoded."}, status=400)
        return

    try:
        ensure_document_size(document_bytes)
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return

    try:
        matter = create_matter_from_document_func(
            filename=filename,
            document_bytes=document_bytes,
            source_type=source_type,
            board_column=board_column,
            intake_metadata=matter_intake_metadata(payload, filename),
        )
    except (DocxExtractionError, PdfExtractionError, ValueError) as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return
    except ParagraphAlignmentError:
        handler._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
        return

    handler._send_json({"matter": matter_view.public_matter(matter)}, status=201)


def matter_intake_metadata(payload: dict, filename: str) -> dict[str, str]:
    sender = clean_intake_text(payload.get("sender"))
    sender = gmail_integration.recipient_email(sender) if sender else ""
    metadata = {
        "sender": sender or "Manual upload",
        "subject": clean_intake_text(payload.get("subject")) or Path(filename).stem or "Untitled NDA",
        "received_at": clean_intake_text(payload.get("received_at")),
        "message_snippet": clean_intake_text(payload.get("message_snippet")) or f"Manual upload of {Path(filename).name or 'NDA document'}.",
        "attachment_filename": clean_intake_text(payload.get("attachment_filename")) or filename,
    }
    reply_to = clean_intake_text(payload.get("reply_to"))
    reply_to = gmail_integration.recipient_email(reply_to) if reply_to else ""
    if reply_to:
        metadata["reply_to"] = reply_to
    return metadata


def clean_intake_text(value: object, max_length: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_length]


def handle_matter_stage_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/stage")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    board_column = payload.get("board_column", "")
    if not isinstance(board_column, str) or board_column not in MATTER_BOARD_COLUMNS:
        handler._send_json({"error": "Unsupported matter stage."}, status=400)
        return

    matter = matter_store.update_matter_stage(matter_id, board_column)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def handle_matter_reviewed_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/reviewed")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    reviewed = payload.get("reviewed", True)
    if not isinstance(reviewed, bool):
        handler._send_json({"error": "reviewed must be true or false."}, status=400)
        return

    matter = matter_store.update_matter_fields(matter_id, {"human_reviewed": reviewed})
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def handle_matter_ai_first_review(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/ai-first-review")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    if not _env_flag_enabled(AI_FIRST_REVIEW_FEATURE_FLAG):
        handler._send_json(
            {
                "error": (
                    "AI-first matter review is disabled. "
                    f"Set {AI_FIRST_REVIEW_FEATURE_FLAG}=true to run it."
                )
            },
            status=403,
        )
        return

    matter = matter_store.get_matter(matter_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    extracted_text = str(matter.get("extracted_text") or "")
    if not extracted_text.strip():
        handler._send_json({"error": "Matter has no extracted text to assess."}, status=400)
        return

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        ai_first_review_result = assess_nda_with_ai(
            extracted_text,
            paragraphs=review_result_paragraphs(matter.get("review_result")),
        )
    except AIAssessorError as error:
        handler._send_json({"error": str(error)}, status=502)
        return
    except (EvidenceProvenanceError, ParagraphAlignmentError, PlaybookTemplateError, ValueError) as error:
        handler._send_json({"error": f"AI-first review could not be completed: {error}"}, status=500)
        return

    completed_at = datetime.now(timezone.utc).isoformat()
    metadata = ai_first_review_store_metadata(
        ai_first_review_result,
        started_at=started_at,
        completed_at=completed_at,
    )
    updated_matter = matter_store.update_matter_ai_first_review(
        matter_id,
        ai_first_review_result,
        metadata,
    )
    if updated_matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({
        "matter": matter_view.public_matter(updated_matter),
        "ai_first_review_metadata": updated_matter.get("ai_first_review_metadata"),
        "ai_first_review_result": ai_first_review_result,
    })


def review_result_paragraphs(review_result: object) -> list[dict] | None:
    if not isinstance(review_result, dict):
        return None
    paragraphs = review_result.get("paragraphs")
    if not isinstance(paragraphs, list):
        return None
    cleaned = [paragraph for paragraph in paragraphs if isinstance(paragraph, dict)]
    return cleaned or None


def ai_first_review_store_metadata(
    ai_first_review_result: dict,
    *,
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    result_metadata = ai_first_review_result.get("ai_first_review")
    if not isinstance(result_metadata, dict):
        result_metadata = {}
    return {
        "status": str(result_metadata.get("status") or "completed"),
        "mode": str(result_metadata.get("mode") or "ai_first_assessor"),
        "provider": str(result_metadata.get("provider") or ""),
        "model": str(result_metadata.get("model") or ""),
        "review_mode": str(ai_first_review_result.get("review_mode") or ""),
        "review_engine_version": ai_first_review_result.get("review_engine_version"),
        "started_at": started_at,
        "completed_at": completed_at,
        "requirements_passed": int(ai_first_review_result.get("requirements_passed") or 0),
        "requirements_needs_review": int(ai_first_review_result.get("requirements_needs_review") or 0),
        "requirements_failed": int(ai_first_review_result.get("requirements_failed") or 0),
    }


def handle_matter_redline_draft_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/redline-draft")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    raw_draft = payload.get("redline_draft")
    if raw_draft is None:
        draft = None
    elif isinstance(raw_draft, dict):
        draft = clean_redline_draft(raw_draft)
    else:
        handler._send_json({"error": "Redline draft must be an object or null."}, status=400)
        return

    matter = matter_store.update_redline_draft(matter_id, draft)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def clean_redline_draft(draft: dict) -> dict:
    manual_redlines = [
        cleaned
        for cleaned in (
            export_service.clean_manual_export_redline(redline)
            for redline in clean_dict_list(draft.get("manual_redline_edits"))
        )
        if cleaned is not None
    ]
    cleaned = {
        "clause_decisions": clean_bool_map(draft.get("clause_decisions")),
        "redline_decisions": clean_bool_map(draft.get("redline_decisions")),
        "template_selections": clean_text_map(draft.get("template_selections")),
        "reviewed_clause_ids": clean_bool_map(draft.get("reviewed_clause_ids")),
        "export_redline_edits": clean_dict_list(draft.get("export_redline_edits")),
        "manual_redline_edits": manual_redlines,
        "review_comments": export_service.clean_review_comments(draft.get("review_comments")),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    cleaned["summary"] = {
        "included_redline_count": len(cleaned["export_redline_edits"]),
        "manual_redline_count": len(cleaned["manual_redline_edits"]),
        "review_comment_count": len(cleaned["review_comments"]),
    }
    return cleaned


def clean_bool_map(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for key, item in list(value.items())[:MAX_REDLINE_DRAFT_ITEMS]:
        key = str(key).strip()[:120]
        if key:
            cleaned[key] = bool(item)
    return cleaned


def clean_text_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for key, item in list(value.items())[:MAX_REDLINE_DRAFT_ITEMS]:
        key = str(key).strip()[:120]
        item = str(item).strip()[:240]
        if key and item:
            cleaned[key] = item
    return cleaned


def clean_dict_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value[:MAX_REDLINE_DRAFT_ITEMS]:
        if not isinstance(item, dict):
            continue
        cleaned.append(json.loads(json.dumps(item)))
    return cleaned


def handle_matter_delete(handler, path: str) -> None:
    matter_id = parse_matter_id(path)
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    matter = matter_store.delete_matter(matter_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"deleted": matter_view.public_matter(matter)})


def handle_demo_reset(handler) -> None:
    removed_count = matter_store.reset_demo_repository()
    handler._send_json({"removed": removed_count, "matters": []})
