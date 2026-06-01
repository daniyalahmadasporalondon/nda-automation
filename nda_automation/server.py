from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .checker import PLAYBOOK_PATH, ParagraphAlignmentError, PlaybookTemplateError, review_nda, validate_playbook
from .document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from .docx_export import DOCX_MIME, DocxExportError
from .docx_text import DocxExtractionError
from . import app_settings, export_service, gmail_integration, matter_view, redline_export_service
from .ingestion_service import (
    create_matter_from_document,
    extract_document,
    is_supported_document_filename,
)
from .pdf_text import PdfExtractionError
from . import matter_store

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
PLAYBOOK_TEMPLATE_ERROR_MESSAGE = "The playbook contains an invalid redline template."
MATTER_SOURCE_COLUMNS = {"gmail_demo": "gmail_demo", "gmail_inbound": "gmail_demo", "manual_upload": "in_review"}
MATTER_BOARD_COLUMNS = {"gmail_demo", "in_review", "redline_ready", "signed_closed"}
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
REQUEST_BODY_TOO_LARGE_MESSAGE = "Request body is larger than the 16 MB limit."
MAX_REDLINE_DRAFT_ITEMS = 200
MAX_OUTBOUND_SUBJECT_CHARS = 240
MAX_OUTBOUND_BODY_CHARS = 10_000
_PLAYBOOK_LOCK = threading.RLock()
_GMAIL_SYNC_LOCK = threading.Lock()
AUTH_REALM = "nda-automation"
AUTH_REQUIRED_MESSAGE = "Authentication required."
AUTH_NOT_CONFIGURED_MESSAGE = "Authentication is required but NDA_AUTH_USERNAME and NDA_AUTH_PASSWORD are not configured."
DURABLE_DATA_DIR_REQUIRED_MESSAGE = "Public deployments must set NDA_DATA_DIR to a durable storage path."
EPHEMERAL_DATA_DIR_MESSAGE = "NDA_DATA_DIR points at ephemeral storage; use a persistent disk or external store."
EPHEMERAL_EXPORTS_DIR_MESSAGE = "NDA_EXPORTS_DIR points at ephemeral storage; use a persistent disk or disable saved export URLs."


@contextmanager
def _locked_playbook():
    with _PLAYBOOK_LOCK:
        PLAYBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_path = PLAYBOOK_PATH.with_suffix(f"{PLAYBOOK_PATH.suffix}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_playbook_atomically(playbook: dict) -> None:
    data = json.dumps(playbook, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    temporary_path = PLAYBOOK_PATH.with_name(f".{PLAYBOOK_PATH.name}.tmp")
    try:
        with temporary_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, PLAYBOOK_PATH)
    except OSError:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _clean_redline_draft(draft: dict) -> dict:
    manual_redlines = [
        cleaned
        for cleaned in (
            export_service.clean_manual_export_redline(redline)
            for redline in _clean_dict_list(draft.get("manual_redline_edits"))
        )
        if cleaned is not None
    ]
    cleaned = {
        "clause_decisions": _clean_bool_map(draft.get("clause_decisions")),
        "template_selections": _clean_text_map(draft.get("template_selections")),
        "export_redline_edits": _clean_dict_list(draft.get("export_redline_edits")),
        "manual_redline_edits": manual_redlines,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    cleaned["summary"] = {
        "included_redline_count": len(cleaned["export_redline_edits"]),
        "manual_redline_count": len(cleaned["manual_redline_edits"]),
    }
    return cleaned


def _clean_bool_map(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for key, item in list(value.items())[:MAX_REDLINE_DRAFT_ITEMS]:
        key = str(key).strip()[:120]
        if key:
            cleaned[key] = bool(item)
    return cleaned


def _clean_text_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for key, item in list(value.items())[:MAX_REDLINE_DRAFT_ITEMS]:
        key = str(key).strip()[:120]
        item = str(item).strip()[:240]
        if key and item:
            cleaned[key] = item
    return cleaned


def _clean_dict_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value[:MAX_REDLINE_DRAFT_ITEMS]:
        if not isinstance(item, dict):
            continue
        cleaned.append(json.loads(json.dumps(item)))
    return cleaned


def _clean_outbound_subject(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:MAX_OUTBOUND_SUBJECT_CHARS]


def _clean_outbound_body(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return None
    return cleaned[:MAX_OUTBOUND_BODY_CHARS]


class NdaAutomationHandler(SimpleHTTPRequestHandler):
    server_version = "nda-automation/0.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._handle_get(send_body=True)

    def do_HEAD(self) -> None:
        self._handle_get(send_body=False)

    def _handle_get(self, *, send_body: bool) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json({"status": "ok"}, send_body=send_body)
            return
        if not self._authorize_request(send_body=send_body):
            return
        exact_routes = {
            "/": lambda: self._send_file(STATIC_DIR / "index.html", send_body=send_body),
            "/playbook": lambda: self._send_file(PLAYBOOK_PATH, "application/json", send_body=send_body),
            "/api/gmail/status": lambda: self._handle_gmail_status(send_body=send_body),
            "/api/matters": lambda: self._handle_matter_list(send_body=send_body),
        }
        handler = exact_routes.get(path)
        if handler is not None:
            handler()
            return
        if path.startswith("/api/matters/") and path.endswith("/review"):
            matter_id = unquote(path.removeprefix("/api/matters/").removesuffix("/review")).strip("/")
            if not matter_id or "/" in matter_id:
                self._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
                return
            try:
                matter = matter_store.get_matter(matter_id)
            except matter_store.MatterStoreError as error:
                self._send_json({"error": str(error)}, status=500)
                return
            if matter is None:
                self._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
                return
            self._send_json(matter_view.review_matter(matter), send_body=send_body)
            return
        if path.startswith("/api/matters/"):
            matter_id = unquote(path.removeprefix("/api/matters/")).strip("/")
            try:
                matter = matter_store.get_matter(matter_id)
            except matter_store.MatterStoreError as error:
                self._send_json({"error": str(error)}, status=500)
                return
            if matter is None:
                self._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
                return
            self._send_json({"matter": matter_view.public_matter(matter)}, send_body=send_body)
            return
        if path.startswith("/static/"):
            requested = (STATIC_DIR / path.removeprefix("/static/")).resolve()
            if STATIC_DIR not in requested.parents or not requested.is_file():
                self._send_json({"error": "Not found"}, status=404, send_body=send_body)
                return
            self._send_file(requested, send_body=send_body)
            return
        if path.startswith("/exports/"):
            if export_service.EXPORTS_DIR is None:
                self._send_json({"error": "Not found"}, status=404, send_body=send_body)
                return
            requested_name = unquote(path.removeprefix("/exports/"))
            requested = (export_service.EXPORTS_DIR / requested_name).resolve()
            if requested.parent != export_service.EXPORTS_DIR.resolve() or not requested.is_file():
                self._send_json({"error": "Not found"}, status=404, send_body=send_body)
                return
            self._send_download(requested.read_bytes(), requested.name, DOCX_MIME, send_body=send_body)
            return
        self._send_json({"error": "Not found"}, status=404, send_body=send_body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authorize_request():
            return
        try:
            exact_routes = {
                "/api/review": self._handle_text_review,
                "/api/review-document": self._handle_document_review,
                "/api/matters": self._handle_matter_upload,
                "/api/gmail/import": self._handle_gmail_import,
                "/api/gmail/send-redline": self._handle_gmail_send_redline,
                "/api/gmail/settings": self._handle_gmail_settings_update,
                "/api/demo/reset": self._handle_demo_reset,
                "/api/export-review-docx": self._handle_review_docx_export,
                "/api/playbook": self._handle_playbook_save,
            }
            handler = exact_routes.get(path)
            if handler is not None:
                handler()
                return
            if path.startswith("/api/matters/") and path.endswith("/stage"):
                self._handle_matter_stage_update(path)
                return
            if path.startswith("/api/matters/") and path.endswith("/redline-draft"):
                self._handle_matter_redline_draft_update(path)
                return
            self._send_json({"error": "Not found"}, status=404)
        except PlaybookTemplateError:
            self._send_playbook_template_error()
        except matter_store.MatterStoreError as error:
            self._send_json({"error": str(error)}, status=500)
        except app_settings.AppSettingsError as error:
            self._send_json({"error": str(error)}, status=500)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if not self._authorize_request():
            return
        try:
            if path.startswith("/api/matters/"):
                self._handle_matter_delete(path)
                return
            self._send_json({"error": "Not found"}, status=404)
        except matter_store.MatterStoreError as error:
            self._send_json({"error": str(error)}, status=500)

    def _handle_matter_list(self, *, send_body: bool = True) -> None:
        try:
            self._send_json({"matters": matter_view.public_matters(matter_store.list_matters())}, send_body=send_body)
        except matter_store.MatterStoreError as error:
            self._send_json({"error": str(error)}, status=500, send_body=send_body)

    def _handle_gmail_status(self, *, send_body: bool = True) -> None:
        try:
            self._send_json({"gmail": gmail_integration.gmail_status()}, send_body=send_body)
        except app_settings.AppSettingsError as error:
            self._send_json({"error": str(error)}, status=500, send_body=send_body)

    def _handle_text_review(self) -> None:
        payload = self._read_json_payload()
        if payload is None:
            return

        text = payload.get("text", "")
        if not isinstance(text, str) or not text.strip():
            self._send_json({"error": "Provide NDA text to review."}, status=400)
            return

        self._send_json(review_nda(text))

    def _handle_document_review(self) -> None:
        payload = self._read_json_payload()
        if payload is None:
            return

        filename = payload.get("filename", "")
        content_base64 = payload.get("content_base64", "")
        if not is_supported_document_filename(filename):
            self._send_json({"error": "Upload a .docx Word document or text-based PDF."}, status=400)
            return
        if not isinstance(content_base64, str) or not content_base64:
            self._send_json({"error": "Provide a document to review."}, status=400)
            return

        try:
            document_bytes = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            self._send_json({"error": "The uploaded document could not be decoded."}, status=400)
            return

        try:
            ensure_document_size(document_bytes)
        except DocumentSizeError:
            self._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
            return

        try:
            source_type, extracted_paragraphs, extraction_quality = extract_document(filename, document_bytes)
        except (DocxExtractionError, PdfExtractionError, ValueError) as error:
            self._send_json({"error": str(error)}, status=400)
            return

        extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted_paragraphs)
        try:
            result = review_nda(extracted_text, paragraphs=extracted_paragraphs)
        except ParagraphAlignmentError:
            self._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
            return
        result["source"] = {
            "filename": filename,
            "type": source_type,
            "extracted_characters": len(extracted_text),
            "extracted_paragraphs": len(extracted_paragraphs),
        }
        if extraction_quality:
            result["source"]["extraction_quality"] = extraction_quality
            warnings = extraction_quality.get("warnings")
            if isinstance(warnings, list) and warnings:
                result.setdefault("review_warnings", []).extend(warnings)
        result["extracted_text"] = extracted_text
        self._send_json(result)

    def _handle_matter_upload(self) -> None:
        payload = self._read_json_payload()
        if payload is None:
            return

        filename = payload.get("filename", "")
        content_base64 = payload.get("content_base64", "")
        source_type = payload.get("source_type", "gmail_demo")
        if not is_supported_document_filename(filename):
            self._send_json({"error": "Upload a .docx Word document or text-based PDF."}, status=400)
            return
        if not isinstance(content_base64, str) or not content_base64:
            self._send_json({"error": "Provide a document to import."}, status=400)
            return
        if not isinstance(source_type, str) or not source_type.strip():
            source_type = "gmail_demo"
        source_type = source_type.strip()
        board_column = MATTER_SOURCE_COLUMNS.get(source_type)
        if board_column is None:
            self._send_json({"error": "Unsupported matter source."}, status=400)
            return

        try:
            document_bytes = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            self._send_json({"error": "The uploaded document could not be decoded."}, status=400)
            return

        try:
            ensure_document_size(document_bytes)
        except DocumentSizeError:
            self._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
            return

        try:
            matter = create_matter_from_document(
                filename=filename,
                document_bytes=document_bytes,
                source_type=source_type,
                board_column=board_column,
                intake_metadata=self._matter_intake_metadata(payload, filename),
            )
        except (DocxExtractionError, PdfExtractionError, ValueError) as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except DocumentSizeError:
            self._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
            return
        except ParagraphAlignmentError:
            self._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
            return

        self._send_json({"matter": matter_view.public_matter(matter)}, status=201)

    def _matter_intake_metadata(self, payload: dict, filename: str) -> dict[str, str]:
        sender = self._clean_intake_text(payload.get("sender"))
        sender = gmail_integration.recipient_email(sender) if sender else ""
        metadata = {
            "sender": sender or "Manual upload",
            "subject": self._clean_intake_text(payload.get("subject")) or Path(filename).stem or "Untitled NDA",
            "received_at": self._clean_intake_text(payload.get("received_at")),
            "message_snippet": (
                self._clean_intake_text(payload.get("message_snippet"))
                or f"Manual upload of {Path(filename).name or 'NDA document'}."
            ),
            "attachment_filename": self._clean_intake_text(payload.get("attachment_filename")) or filename,
        }
        reply_to = self._clean_intake_text(payload.get("reply_to"))
        reply_to = gmail_integration.recipient_email(reply_to) if reply_to else ""
        if reply_to:
            metadata["reply_to"] = reply_to
        for field in ("gmail_account", "gmail_attachment_id", "gmail_attachment_sha256", "gmail_message_id", "gmail_part_id", "gmail_thread_id"):
            value = self._clean_intake_text(payload.get(field))
            if value:
                metadata[field] = value
        return metadata

    @staticmethod
    def _clean_intake_text(value: object, max_length: int = 500) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:max_length]

    def _handle_matter_stage_update(self, path: str) -> None:
        matter_id = unquote(path.removeprefix("/api/matters/").removesuffix("/stage")).strip("/")
        if not matter_id or "/" in matter_id:
            self._send_json({"error": "Matter not found."}, status=404)
            return

        payload = self._read_json_payload()
        if payload is None:
            return

        board_column = payload.get("board_column", "")
        if not isinstance(board_column, str) or board_column not in MATTER_BOARD_COLUMNS:
            self._send_json({"error": "Unsupported matter stage."}, status=400)
            return

        matter = matter_store.update_matter_stage(matter_id, board_column)
        if matter is None:
            self._send_json({"error": "Matter not found."}, status=404)
            return
        self._send_json({"matter": matter_view.public_matter(matter)})

    def _handle_matter_redline_draft_update(self, path: str) -> None:
        matter_id = unquote(path.removeprefix("/api/matters/").removesuffix("/redline-draft")).strip("/")
        if not matter_id or "/" in matter_id:
            self._send_json({"error": "Matter not found."}, status=404)
            return

        payload = self._read_json_payload()
        if payload is None:
            return

        raw_draft = payload.get("redline_draft")
        if raw_draft is None:
            draft = None
        elif isinstance(raw_draft, dict):
            draft = _clean_redline_draft(raw_draft)
        else:
            self._send_json({"error": "Redline draft must be an object or null."}, status=400)
            return

        matter = matter_store.update_redline_draft(matter_id, draft)
        if matter is None:
            self._send_json({"error": "Matter not found."}, status=404)
            return
        self._send_json({"matter": matter_view.public_matter(matter)})

    def _handle_matter_delete(self, path: str) -> None:
        matter_id = unquote(path.removeprefix("/api/matters/")).strip("/")
        if not matter_id or "/" in matter_id:
            self._send_json({"error": "Matter not found."}, status=404)
            return

        matter = matter_store.delete_matter(matter_id)
        if matter is None:
            self._send_json({"error": "Matter not found."}, status=404)
            return
        self._send_json({"deleted": matter_view.public_matter(matter)})

    def _handle_gmail_import(self) -> None:
        self._send_json({"error": "Manual Gmail sync is disabled. Use Admin sync frequency."}, status=410)

    def _handle_gmail_settings_update(self) -> None:
        payload = self._read_json_payload()
        if payload is None:
            return

        updates: dict[str, object] = {}
        for key in ("inbound_enabled", "outbound_enabled"):
            if key not in payload:
                continue
            value = payload.get(key)
            if not isinstance(value, bool):
                self._send_json({"error": "Gmail enabled settings must be true or false."}, status=400)
                return
            updates[key] = value
        if "sync_cadence" in payload:
            self._send_json({"error": "Use sync_frequency for Gmail sync frequency."}, status=400)
            return
        if "sync_frequency" in payload:
            sync_frequency = payload.get("sync_frequency")
            if not isinstance(sync_frequency, str) or sync_frequency not in app_settings.GMAIL_SYNC_FREQUENCIES:
                self._send_json({"error": "Unsupported Gmail sync frequency."}, status=400)
                return
            updates["sync_frequency"] = sync_frequency
        if not updates:
            self._send_json({"error": "Provide a Gmail setting to update."}, status=400)
            return

        settings = app_settings.update_gmail_settings(updates)
        self._send_json({"gmail_settings": settings, "gmail": gmail_integration.gmail_status()})

    def _handle_demo_reset(self) -> None:
        removed_count = matter_store.reset_demo_repository()
        self._send_json({"removed": removed_count, "matters": []})

    def _handle_gmail_send_redline(self) -> None:
        payload = self._read_json_payload()
        if payload is None:
            return

        matter_id = payload.get("matter_id")
        if not isinstance(matter_id, str) or not matter_id.strip():
            self._send_json({"error": "Matter not found."}, status=404)
            return
        if payload.get("confirm_send") is not True:
            self._send_json({"error": "Confirm send is required before emailing a redline."}, status=400)
            return

        matter = matter_store.get_matter(matter_id.strip())
        if matter is None:
            self._send_json({"error": "Matter not found."}, status=404)
            return
        if not gmail_integration.matter_reply_recipient(matter):
            self._send_json({"error": "Matter does not have a valid reply recipient email address."}, status=400)
            return
        if not app_settings.gmail_role_enabled("outbound"):
            self._send_json({"error": "Gmail outbound is disabled in Admin."}, status=409)
            return
        outbound_subject = _clean_outbound_subject(payload.get("subject"))
        outbound_body = _clean_outbound_body(payload.get("body"))

        try:
            gmail_integration.validate_outbound_send_ready(matter)
        except gmail_integration.GmailIntegrationError as error:
            self._send_json({"error": str(error)}, status=_gmail_send_error_status(error))
            return

        try:
            redline_export = redline_export_service.build_matter_redline(matter_id.strip(), payload)
        except redline_export_service.DocxOpenHealthError as error:
            self._send_json({"error": str(error), "details": error.details}, status=500)
            return
        except DocxExtractionError as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except DocxExportError as error:
            self._send_json({"error": str(error)}, status=400)
            return

        try:
            sent = gmail_integration.send_redline_email(
                matter,
                redline_export.data,
                redline_export.filename,
                body=outbound_body,
                subject=outbound_subject,
            )
        except gmail_integration.GmailIntegrationError as error:
            self._send_json({"error": str(error)}, status=_gmail_send_error_status(error))
            return

        updated_matter = matter_store.update_matter_fields(
            matter_id.strip(),
            {
                "board_column": "redline_ready",
                "last_outbound_account": sent.get("outbound_account", ""),
                "last_outbound_at": sent.get("sent_at", ""),
                "last_outbound_filename": redline_export.filename,
                "last_outbound_message_id": sent.get("message_id", ""),
                "last_outbound_subject": sent.get("subject", ""),
                "last_outbound_thread_id": sent.get("thread_id", ""),
                "last_outbound_to": sent.get("to", ""),
                "status": "active",
            },
        )
        if updated_matter is None:
            self._send_json({"error": "Matter not found."}, status=404)
            return
        self._send_json({
            "filename": redline_export.filename,
            "matter": matter_view.public_matter(updated_matter),
            "sent": sent,
        })

    def _handle_review_docx_export(self) -> None:
        payload = self._read_json_payload()
        if payload is None:
            return

        text = payload.get("text", "")
        reviewed_text = payload.get("reviewed_text", "")
        has_docx_payload = (
            isinstance(payload.get("filename"), str)
            and payload.get("filename", "").lower().endswith(".docx")
            and isinstance(payload.get("content_base64"), str)
            and bool(payload.get("content_base64"))
        )
        export_text = reviewed_text if isinstance(reviewed_text, str) and reviewed_text.strip() else text
        has_matter_payload = isinstance(payload.get("matter_id"), str) and bool(payload.get("matter_id", "").strip())
        if (not isinstance(export_text, str) or not export_text.strip()) and not has_docx_payload and not has_matter_payload:
            self._send_json({"error": "Provide NDA text to export."}, status=400)
            return
        if (
            not has_matter_payload
            and not has_docx_payload
            and isinstance(text, str)
            and text.strip()
            and isinstance(reviewed_text, str)
            and reviewed_text.strip()
            and text.strip() != reviewed_text.strip()
        ):
            self._send_json({"error": "Export text must match the latest reviewed text. Run Review NDA again."}, status=409)
            return

        title = payload.get("title", "NDA Review")
        if not isinstance(title, str) or not title.strip():
            title = "NDA Review"

        try:
            if has_matter_payload:
                redline_export = redline_export_service.build_matter_redline(
                    str(payload.get("matter_id", "")).strip(),
                    payload,
                    persist=True,
                )
            else:
                redline_export = redline_export_service.build_review_export(payload, export_text, title=title)
        except redline_export_service.DocxOpenHealthError as error:
            self._send_json({
                "error": str(error),
                "details": error.details,
            }, status=500)
            return
        except DocxExtractionError as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except PdfExtractionError as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except ParagraphAlignmentError:
            self._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
            return
        except DocxExportError as error:
            self._send_json({"error": str(error)}, status=400)
            return

        headers = {"X-Export-Verified": redline_export_service.VERIFIED_EXPORT_HEADER}
        if redline_export.saved_path is not None:
            headers.update({
                "X-Export-URL": f"/exports/{quote(redline_export.saved_path.name)}",
            })
        self._send_download(
            redline_export.data,
            redline_export.filename,
            DOCX_MIME,
            headers=headers,
        )

    def _handle_playbook_save(self) -> None:
        payload = self._read_json_payload()
        if payload is None:
            return

        playbook = payload.get("playbook")
        if not isinstance(playbook, dict):
            self._send_json({"error": "Playbook payload must include a playbook object."}, status=400)
            return

        try:
            with _locked_playbook():
                validate_playbook(playbook)
                _write_playbook_atomically(playbook)
        except PlaybookTemplateError as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except OSError:
            self._send_json({"error": "Playbook could not be saved."}, status=500)
            return

        self._send_json({"playbook": playbook, "saved_at": datetime.now(timezone.utc).isoformat()})

    def _read_json_payload(self) -> dict | None:
        content_length = self._read_content_length()
        if content_length is None:
            return None
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "Request body must be valid JSON."}, status=400)
            return None
        if not isinstance(payload, dict):
            self._send_json({"error": "Request body must be a JSON object."}, status=400)
            return None
        return payload

    def _read_content_length(self) -> int | None:
        raw_content_length = self.headers.get("Content-Length")
        if raw_content_length is None:
            return 0
        raw_content_length = raw_content_length.strip()
        if not raw_content_length:
            self._send_json({"error": "Content-Length must be a non-negative integer."}, status=400)
            return None
        try:
            content_length = int(raw_content_length)
        except ValueError:
            self._send_json({"error": "Content-Length must be a non-negative integer."}, status=400)
            return None
        if content_length < 0:
            self._send_json({"error": "Content-Length must be a non-negative integer."}, status=400)
            return None
        if content_length > MAX_REQUEST_BODY_BYTES:
            self._send_json({"error": REQUEST_BODY_TOO_LARGE_MESSAGE}, status=413)
            return None
        return content_length

    def _send_file(self, path: Path, content_type: str | None = None, *, send_body: bool = True) -> None:
        if not path.is_file():
            self._send_json({"error": "Not found"}, status=404, send_body=send_body)
            return
        data = path.read_bytes()
        etag = f'"sha256-{hashlib.sha256(data).hexdigest()}"'
        detected_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", detected_type)
        self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _send_download(
        self,
        data: bytes,
        filename: str,
        content_type: str,
        headers: dict[str, str] | None = None,
        *,
        send_body: bool = True,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for header, value in (headers or {}).items():
            self.send_header(header, value)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _send_json(
        self,
        payload: dict,
        status: int = 200,
        headers: dict[str, str] | None = None,
        *,
        send_body: bool = True,
    ) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for header, value in (headers or {}).items():
            self.send_header(header, value)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _send_playbook_template_error(self) -> None:
        self._send_json({"error": PLAYBOOK_TEMPLATE_ERROR_MESSAGE}, status=500)

    def _authorize_request(self, *, send_body: bool = True) -> bool:
        if not _auth_required_for_host(str(self.server.server_address[0])):
            return True
        username = os.environ.get("NDA_AUTH_USERNAME", "").strip()
        password = os.environ.get("NDA_AUTH_PASSWORD", "")
        if not username or not password:
            self._send_json({"error": AUTH_NOT_CONFIGURED_MESSAGE}, status=503, send_body=send_body)
            return False
        if _basic_auth_matches(self.headers.get("Authorization", ""), username, password):
            return True
        self._send_json(
            {"error": AUTH_REQUIRED_MESSAGE},
            status=401,
            headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}", charset="UTF-8"'},
            send_body=send_body,
        )
        return False


def _basic_auth_matches(header: str, username: str, password: str) -> bool:
    prefix = "Basic "
    if not header.startswith(prefix):
        return False
    try:
        decoded = base64.b64decode(header[len(prefix) :], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    supplied_username, separator, supplied_password = decoded.partition(":")
    if not separator:
        return False
    return hmac.compare_digest(supplied_username, username) and hmac.compare_digest(supplied_password, password)


def _auth_required_for_host(host: str) -> bool:
    if _env_flag_enabled("NDA_REQUIRE_AUTH"):
        return True
    if os.environ.get("NDA_AUTH_USERNAME") or os.environ.get("NDA_AUTH_PASSWORD"):
        return True
    return not _is_loopback_host(host)


def _validate_public_auth(host: str) -> None:
    if not _auth_required_for_host(host):
        return
    if not os.environ.get("NDA_AUTH_USERNAME", "").strip() or not os.environ.get("NDA_AUTH_PASSWORD", ""):
        raise RuntimeError(AUTH_NOT_CONFIGURED_MESSAGE)


def _validate_public_storage(host: str) -> None:
    if _is_loopback_host(host) or _env_flag_enabled("NDA_ALLOW_EPHEMERAL_DATA"):
        return
    if not os.environ.get("NDA_DATA_DIR"):
        raise RuntimeError(DURABLE_DATA_DIR_REQUIRED_MESSAGE)
    if _is_ephemeral_storage_path(matter_store.DATA_DIR):
        raise RuntimeError(EPHEMERAL_DATA_DIR_MESSAGE)
    if export_service.EXPORTS_DIR is not None and _is_ephemeral_storage_path(export_service.EXPORTS_DIR):
        raise RuntimeError(EPHEMERAL_EXPORTS_DIR_MESSAGE)


def _is_ephemeral_storage_path(path: Path) -> bool:
    try:
        resolved_path = path.expanduser().resolve(strict=False)
    except OSError:
        resolved_path = path.expanduser().absolute()
    ephemeral_roots = {
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/tmp"),
        Path(tempfile.gettempdir()).expanduser().resolve(strict=False),
    }
    for root in ephemeral_roots:
        try:
            resolved_root = root.resolve(strict=False)
        except OSError:
            resolved_root = root.absolute()
        if resolved_path == resolved_root or resolved_root in resolved_path.parents:
            return True
    return False


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _gmail_send_error_status(error: Exception) -> int:
    message = str(error).lower()
    if "valid reply recipient" in message:
        return 400
    conflict_markers = (
        "disabled in admin",
        "does not match inbound gmail account",
        "mismatch",
        "self-sent gmail message",
        "gmail outbound profile",
        "set nda_gmail_outbound_token_path",
        "gmail outbound token",
    )
    if any(marker in message for marker in conflict_markers):
        return 409
    return 503


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the nda-automation local app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    args = parser.parse_args()
    try:
        _validate_public_auth(args.host)
        _validate_public_storage(args.host)
    except RuntimeError as error:
        parser.error(str(error))

    server = ThreadingHTTPServer((args.host, args.port), NdaAutomationHandler)
    _start_gmail_sync_scheduler()
    print(f"nda-automation running at http://{args.host}:{args.port}")
    server.serve_forever()


def _start_gmail_sync_scheduler() -> None:
    scheduler = threading.Thread(target=_gmail_sync_scheduler_loop, daemon=True)
    scheduler.start()


@contextmanager
def _gmail_sync_process_lock():
    if not _GMAIL_SYNC_LOCK.acquire(blocking=False):
        yield False
        return
    lock_file = None
    locked = False
    try:
        matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = (matter_store.DATA_DIR / "gmail_sync.lock").open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            locked = True
        yield True
    finally:
        if lock_file is not None:
            if locked and fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        _GMAIL_SYNC_LOCK.release()


def _gmail_sync_scheduler_loop() -> None:
    last_run = 0.0
    last_frequency = ""
    while True:
        try:
            settings = app_settings.gmail_settings()
            frequency = str(settings.get("sync_frequency") or app_settings.DEFAULT_GMAIL_SETTINGS["sync_frequency"])
            interval_seconds = app_settings.gmail_sync_interval_seconds(frequency)
            if frequency != last_frequency:
                last_run = 0.0
                last_frequency = frequency
            if settings.get("inbound_enabled", True):
                now = time.monotonic()
                if now - last_run >= interval_seconds:
                    with _gmail_sync_process_lock() as lock_acquired:
                        if not lock_acquired:
                            last_run = now
                            continue
                        try:
                            _run_scheduled_gmail_sync()
                        finally:
                            last_run = now
        except Exception as error:  # pragma: no cover - defensive background logging.
            print(f"Gmail sync scheduler failed: {error}")


def _run_scheduled_gmail_sync() -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = gmail_integration.import_inbound_matters(limit=gmail_integration.MAX_GMAIL_IMPORT_LIMIT)
        result = {**result, "deduplicated_count": matter_store.deduplicate_gmail_matters()}
        finished_at = datetime.now(timezone.utc).isoformat()
        app_settings.record_gmail_sync(result, synced_at=finished_at, started_at=started_at, finished_at=finished_at)
    except Exception as error:  # pragma: no cover - defensive background logging.
        finished_at = datetime.now(timezone.utc).isoformat()
        app_settings.record_gmail_sync_error(
            str(error),
            started_at=started_at,
            finished_at=finished_at,
            query=gmail_integration.DEFAULT_INBOUND_QUERY,
        )
        print(f"Gmail scheduled sync failed: {error}")
        time.sleep(5)


if __name__ == "__main__":
    main()
