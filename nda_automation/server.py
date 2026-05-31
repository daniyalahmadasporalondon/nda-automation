from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .checker import PLAYBOOK_PATH, ParagraphAlignmentError, PlaybookTemplateError, review_nda
from .docx_export import DOCX_MIME, DocxExportError, build_review_report_docx, build_source_redline_docx
from .docx_text import DocxExtractionError, extract_docx_paragraphs

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
EXPORTS_DIR = Path(os.environ["NDA_EXPORTS_DIR"]).expanduser() if os.environ.get("NDA_EXPORTS_DIR") else None
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_SAVED_EXPORTS = 25
PLAYBOOK_TEMPLATE_ERROR_MESSAGE = "The playbook contains an invalid redline template."


class NdaAutomationHandler(SimpleHTTPRequestHandler):
    server_version = "nda-automation/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print("%s - - %s" % (self.address_string(), format % args))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(STATIC_DIR / "index.html")
            return
        if path == "/playbook":
            self._send_file(PLAYBOOK_PATH, "application/json")
            return
        if path == "/api/health":
            self._send_json({"status": "ok"})
            return
        if path.startswith("/static/"):
            requested = (STATIC_DIR / path.removeprefix("/static/")).resolve()
            if STATIC_DIR not in requested.parents or not requested.is_file():
                self._send_json({"error": "Not found"}, status=404)
                return
            self._send_file(requested)
            return
        if path.startswith("/exports/"):
            if EXPORTS_DIR is None:
                self._send_json({"error": "Not found"}, status=404)
                return
            requested_name = unquote(path.removeprefix("/exports/"))
            requested = (EXPORTS_DIR / requested_name).resolve()
            if requested.parent != EXPORTS_DIR.resolve() or not requested.is_file():
                self._send_json({"error": "Not found"}, status=404)
                return
            self._send_download(requested.read_bytes(), requested.name, DOCX_MIME)
            return
        self._send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/review":
                self._handle_text_review()
                return
            if path == "/api/review-document":
                self._handle_document_review()
                return
            if path == "/api/export-review-docx":
                self._handle_review_docx_export()
                return
            self._send_json({"error": "Not found"}, status=404)
        except PlaybookTemplateError:
            self._send_playbook_template_error()

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
        if not isinstance(filename, str) or not filename.lower().endswith(".docx"):
            self._send_json({"error": "Upload a .docx Word document."}, status=400)
            return
        if not isinstance(content_base64, str) or not content_base64:
            self._send_json({"error": "Provide a Word document to review."}, status=400)
            return

        try:
            document_bytes = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            self._send_json({"error": "The uploaded Word document could not be decoded."}, status=400)
            return

        if len(document_bytes) > MAX_DOCUMENT_BYTES:
            self._send_json({"error": "The Word document is larger than the 10 MB upload limit."}, status=400)
            return

        try:
            extracted_paragraphs = extract_docx_paragraphs(document_bytes)
        except DocxExtractionError as error:
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
            "type": "docx",
            "extracted_characters": len(extracted_text),
            "extracted_paragraphs": len(extracted_paragraphs),
        }
        result["extracted_text"] = extracted_text
        self._send_json(result)

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
        if (not isinstance(export_text, str) or not export_text.strip()) and not has_docx_payload:
            self._send_json({"error": "Provide NDA text to export."}, status=400)
            return
        if (
            not has_docx_payload
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
            review_result, source_document_bytes, source_filename = self._review_result_for_export(payload, export_text)
        except DocxExtractionError as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except ParagraphAlignmentError:
            self._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
            return

        _apply_selected_export_redlines(review_result, payload.get("export_redline_edits"))
        _apply_manual_export_redlines(review_result, payload.get("manual_redline_edits"))

        if source_document_bytes is not None:
            try:
                report_bytes = build_source_redline_docx(source_document_bytes, review_result)
            except DocxExportError as error:
                self._send_json({"error": str(error)}, status=400)
                return
            download_filename = _redline_download_filename(source_filename)
        else:
            report_bytes = build_review_report_docx(review_result, title=title.strip())
            download_filename = "nda-review-report.docx"
        saved_path = _persist_export(report_bytes, download_filename)
        headers = {}
        if saved_path is not None:
            headers = {
                "X-Export-Path": str(saved_path),
                "X-Export-URL": f"/exports/{quote(saved_path.name)}",
            }
        self._send_download(
            report_bytes,
            download_filename,
            DOCX_MIME,
            headers=headers,
        )

    def _review_result_for_export(self, payload: dict, fallback_text: str) -> tuple[dict, bytes | None, str]:
        filename = payload.get("filename", "")
        content_base64 = payload.get("content_base64", "")
        if isinstance(filename, str) and filename.lower().endswith(".docx") and isinstance(content_base64, str) and content_base64:
            try:
                document_bytes = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise DocxExtractionError("The uploaded Word document could not be decoded.") from exc

            if len(document_bytes) > MAX_DOCUMENT_BYTES:
                raise DocxExtractionError("The Word document is larger than the 10 MB upload limit.")

            extracted_paragraphs = extract_docx_paragraphs(document_bytes)
            extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted_paragraphs)
            return review_nda(extracted_text, paragraphs=extracted_paragraphs), document_bytes, filename

        return review_nda(fallback_text), None, ""

    def _read_json_payload(self) -> dict | None:
        content_length = int(self.headers.get("Content-Length", "0"))
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

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self._send_json({"error": "Not found"}, status=404)
            return
        data = path.read_bytes()
        detected_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", detected_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_download(self, data: bytes, filename: str, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for header, value in (headers or {}).items():
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_playbook_template_error(self) -> None:
        self._send_json({"error": PLAYBOOK_TEMPLATE_ERROR_MESSAGE}, status=500)


def _redline_download_filename(filename: str) -> str:
    source_name = Path(filename).stem if filename else ""
    safe_name = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in source_name)
    safe_name = safe_name.strip("-_") or "nda"
    return f"{safe_name}-redlined.docx"


def _apply_selected_export_redlines(review_result: dict, selected_redlines: object) -> None:
    if not isinstance(selected_redlines, list):
        return

    cleaned_redlines = [
        redline
        for redline in (_clean_export_redline(item) for item in selected_redlines)
        if redline is not None
    ]
    review_result["redline_edits"] = cleaned_redlines


def _apply_manual_export_redlines(review_result: dict, manual_redlines: object) -> None:
    if not isinstance(manual_redlines, list):
        return

    cleaned_redlines = [
        redline
        for redline in (_clean_manual_export_redline(item) for item in manual_redlines)
        if redline is not None
    ]
    if not cleaned_redlines:
        return

    manual_paragraph_ids = {str(redline.get("paragraph_id")) for redline in cleaned_redlines}
    existing_redlines = review_result.get("redline_edits", [])
    if not isinstance(existing_redlines, list):
        existing_redlines = []
    review_result["redline_edits"] = cleaned_redlines + [
        redline
        for redline in existing_redlines
        if not (isinstance(redline, dict) and str(redline.get("paragraph_id")) in manual_paragraph_ids)
    ]


def _clean_manual_export_redline(redline: object) -> dict | None:
    if not isinstance(redline, dict):
        return None

    common = _clean_export_redline_contract(redline, {"replace_paragraph", "delete_paragraph"})
    if common is None:
        return None

    action = common["action"]
    paragraph_id = common["paragraph_id"]

    cleaned = {
        "id": str(redline.get("id") or f"manual-{paragraph_id}"),
        "clause_id": "manual_viewer_edit",
        "status": "proposed",
        "action": action,
        "action_label": "Remove paragraph" if action == "delete_paragraph" else "Replace paragraph",
        "paragraph_id": paragraph_id,
        "original_text": common["original_text"],
        "replacement_text": common["replacement_text"],
    }

    _copy_redline_indexes(redline, cleaned)
    return cleaned


def _clean_export_redline(redline: object) -> dict | None:
    if not isinstance(redline, dict):
        return None

    common = _clean_export_redline_contract(
        redline,
        {"replace_paragraph", "delete_paragraph", "insert_after_paragraph"},
    )
    if common is None:
        return None

    cleaned = {
        key: value
        for key, value in redline.items()
        if key in {
            "id",
            "clause_id",
            "clause_name",
            "paragraph_id",
            "paragraph_index",
            "source_index",
            "action",
            "action_label",
            "status",
            "original_text",
            "replacement_text",
            "reason",
            "target_position",
            "anchor_text",
            "insert_text",
            "selected_template_id",
            "template_options",
        }
    }
    cleaned.update(common)
    _copy_redline_indexes(redline, cleaned, remove_invalid=True)
    return cleaned


def _clean_export_redline_contract(redline: dict, allowed_actions: set[str]) -> dict | None:
    action = redline.get("action")
    if action not in allowed_actions:
        return None

    paragraph_id = str(redline.get("paragraph_id") or "")
    if not paragraph_id:
        return None

    original_text = str(redline.get("original_text") or "")
    replacement_text = str(redline.get("replacement_text") or "")
    anchor_text = str(redline.get("anchor_text") or "")
    insert_text = str(redline.get("insert_text") or "")
    if action in {"replace_paragraph", "delete_paragraph"} and not original_text.strip():
        return None
    if action == "replace_paragraph" and not replacement_text.strip():
        return None
    if action == "insert_after_paragraph" and not insert_text.strip():
        return None

    return {
        "action": action,
        "paragraph_id": paragraph_id,
        "original_text": original_text,
        "replacement_text": "" if action == "delete_paragraph" else replacement_text,
        "anchor_text": anchor_text,
        "insert_text": insert_text,
    }


def _copy_redline_indexes(source: dict, target: dict, *, remove_invalid: bool = False) -> None:
    for key in ("paragraph_index", "source_index"):
        try:
            target[key] = int(source.get(key))
        except (TypeError, ValueError, KeyError):
            if remove_invalid:
                target.pop(key, None)


def _persist_export(data: bytes, filename: str) -> Path | None:
    if EXPORTS_DIR is None:
        return None
    safe_name = os.path.basename(filename) or "nda-review-report.docx"
    try:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        export_path = (EXPORTS_DIR / safe_name).resolve()
        if export_path.parent != EXPORTS_DIR.resolve():
            export_path = EXPORTS_DIR / "nda-review-report.docx"
        export_path.write_bytes(data)
        _prune_saved_exports(export_path)
        return export_path
    except OSError as error:
        print(f"Could not save export copy: {error}")
        return None


def _prune_saved_exports(protected_path: Path) -> None:
    if EXPORTS_DIR is None:
        return
    saved_exports = [
        path
        for path in EXPORTS_DIR.glob("*.docx")
        if path.is_file()
    ]
    if len(saved_exports) <= MAX_SAVED_EXPORTS:
        return

    protected_path = protected_path.resolve()
    saved_exports.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    removable_exports = [path for path in saved_exports[MAX_SAVED_EXPORTS:] if path.resolve() != protected_path]
    for path in removable_exports:
        path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the nda-automation local app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), NdaAutomationHandler)
    print(f"nda-automation running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
