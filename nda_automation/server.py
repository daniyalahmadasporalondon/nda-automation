from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .checker import PLAYBOOK_PATH, review_nda
from .docx_text import DocxExtractionError, extract_docx_paragraphs

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


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
        self._send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/review":
            self._handle_text_review()
            return
        if path == "/api/review-document":
            self._handle_document_review()
            return
        self._send_json({"error": "Not found"}, status=404)

    def _handle_text_review(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "Request body must be valid JSON."}, status=400)
            return

        text = payload.get("text", "")
        if not isinstance(text, str) or not text.strip():
            self._send_json({"error": "Provide NDA text to review."}, status=400)
            return

        self._send_json(review_nda(text))

    def _handle_document_review(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "Request body must be valid JSON."}, status=400)
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
        result = review_nda(extracted_text, paragraphs=extracted_paragraphs)
        result["source"] = {
            "filename": filename,
            "type": "docx",
            "extracted_characters": len(extracted_text),
            "extracted_paragraphs": len(extracted_paragraphs),
        }
        result["extracted_text"] = extracted_text
        self._send_json(result)

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

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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
