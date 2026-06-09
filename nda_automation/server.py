from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import app_settings, export_service, gmail_integration, matter_store, redline_export_service as redline_export_service, telemetry, user_store
from .checker import (
    PLAYBOOK_PATH,
    ai_validate_draft_fix,
    EvidenceProvenanceError,
    PlaybookTemplateError,
    ai_second_opinion_for_clause,
)
from .deployment import (
    DURABLE_DATA_DIR_REQUIRED_MESSAGE as DURABLE_DATA_DIR_REQUIRED_MESSAGE,
    EPHEMERAL_DATA_DIR_MESSAGE as EPHEMERAL_DATA_DIR_MESSAGE,
    EPHEMERAL_EXPORTS_DIR_MESSAGE as EPHEMERAL_EXPORTS_DIR_MESSAGE,
    EPHEMERAL_USERS_PATH_MESSAGE as EPHEMERAL_USERS_PATH_MESSAGE,
    _deployment_status_for_host as _deployment_status_for_host,
    _is_ephemeral_storage_path as _is_ephemeral_storage_path,
    _validate_public_auth,
    _validate_public_storage,
)
from .csrf import (
    CSRF_REJECTED_MESSAGE,
    origin_allowed_for_request,
)
from .docx_export import DOCX_MIME
from .http_auth import (
    ADMIN_REQUIRED_MESSAGE as ADMIN_REQUIRED_MESSAGE,
    AUTH_NOT_CONFIGURED_MESSAGE,
    AUTH_REALM,
    AUTH_REQUIRED_MESSAGE,
    HOST_NOT_ALLOWED_MESSAGE,
    _auth_method_configured,
    _basic_auth_credentials,
    _basic_auth_configured,
    _auth_required_for_host,
    _basic_auth_matches,
    _google_oauth_configured,
    _env_flag_enabled as _env_flag_enabled,
    _is_loopback_host as _is_loopback_host,
    host_header_allowed,
)
from .rate_limit import (
    DEFAULT_RATE_LIMIT_PER_MINUTE as DEFAULT_RATE_LIMIT_PER_MINUTE,
    RATE_LIMITED_MESSAGE,
    _rate_limit_bucket_name as _rate_limit_bucket_name,
    _rate_limit_client_key,
    _rate_limit_per_window as _rate_limit_per_window,
    _rate_limit_retry_after,
    _rate_limit_window_seconds as _rate_limit_window_seconds,
    _reset_rate_limits as _reset_rate_limits,
)
from .ingestion_service import create_matter_from_document, extract_document
from .review_engine import review_nda_with_active_engine
from .routes import admin as admin_routes
from .routes import approval as approval_routes
from .routes import auth as auth_routes
from .routes import dashboard as dashboard_routes
from .routes import drive as drive_routes
from .routes import entities as entity_routes
from .routes import fill as fill_routes
from .routes import generation as generation_routes
from .routes import gmail as gmail_routes
from .routes import matters as matter_routes
from .routes import pdf_markup as pdf_markup_routes
from .routes import playbook as playbook_routes
from .routes import review as review_routes
from .routes import send_document as send_document_routes
from .routes.common import parse_matter_id as _route_parse_matter_id

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
PLAYBOOK_TEMPLATE_ERROR_MESSAGE = "The playbook contains an invalid redline template."
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
REQUEST_BODY_TOO_LARGE_MESSAGE = "Request body is larger than the 16 MB limit."
MAX_GMAIL_SYNC_IDLE_SECONDS = 30
_GMAIL_SYNC_LOCK = threading.Lock()
_GMAIL_SYNC_BACKOFF_UNTIL = 0.0


def _parse_matter_id(path: str, *, suffix: str = "") -> str | None:
    return _route_parse_matter_id(path, suffix=suffix)


def _handle_index_get(handler, *, send_body: bool) -> None:
    handler._send_file(STATIC_DIR / "index.html", send_body=send_body)


def _handle_playbook_get(handler, *, send_body: bool) -> None:
    handler._send_file(PLAYBOOK_PATH, "application/json", send_body=send_body)


def _handle_playbook_api_get(handler, *, send_body: bool) -> None:
    playbook_routes.handle_playbook_get(handler, playbook_path=PLAYBOOK_PATH, send_body=send_body)


def _handle_playbook_draft_get(handler, *, send_body: bool) -> None:
    playbook_routes.handle_playbook_draft_get(handler, playbook_path=PLAYBOOK_PATH, send_body=send_body)


def _handle_text_review_post(handler) -> None:
    review_routes.handle_text_review(handler, review_nda_func=review_nda_with_active_engine)


def _handle_document_review_post(handler) -> None:
    review_routes.handle_document_review(
        handler,
        extract_document_func=extract_document,
        review_nda_func=review_nda_with_active_engine,
    )


def _handle_ai_second_opinion_post(handler) -> None:
    review_routes.handle_ai_second_opinion(
        handler,
        second_opinion_func=ai_second_opinion_for_clause,
    )


def _handle_ai_draft_validation_post(handler) -> None:
    review_routes.handle_ai_draft_validation(
        handler,
        validation_func=ai_validate_draft_fix,
    )


def _handle_matter_upload_post(handler) -> None:
    matter_routes.handle_matter_upload(
        handler,
        create_matter_from_document_func=create_matter_from_document,
    )


def _handle_send_document_post(handler) -> None:
    send_document_routes.handle_send_document(handler)


def _handle_playbook_save_post(handler) -> None:
    playbook_routes.handle_playbook_save(
        handler,
        playbook_path=PLAYBOOK_PATH,
        replace_file=os.replace,
    )


def _handle_playbook_restore_post(handler) -> None:
    playbook_routes.handle_playbook_restore(
        handler,
        playbook_path=PLAYBOOK_PATH,
        replace_file=os.replace,
    )


def _handle_playbook_draft_save_post(handler) -> None:
    playbook_routes.handle_playbook_draft_save(
        handler,
        playbook_path=PLAYBOOK_PATH,
        replace_file=os.replace,
    )


def _handle_playbook_validate_draft_post(handler) -> None:
    playbook_routes.handle_playbook_validate_draft(handler, playbook_path=PLAYBOOK_PATH)


def _handle_playbook_draft_discard_post(handler) -> None:
    playbook_routes.handle_playbook_draft_discard(
        handler,
        playbook_path=PLAYBOOK_PATH,
        replace_file=os.replace,
    )


def _handle_playbook_publish_post(handler) -> None:
    playbook_routes.handle_playbook_publish(
        handler,
        playbook_path=PLAYBOOK_PATH,
        replace_file=os.replace,
    )


_GET_EXACT_ROUTES = {
    "/": _handle_index_get,
    "/api/deployment/status": admin_routes.handle_deployment_status,
    "/playbook": _handle_playbook_get,
    "/api/playbook": _handle_playbook_api_get,
    "/api/playbook/draft": _handle_playbook_draft_get,
    "/api/gmail/status": gmail_routes.handle_gmail_status,
    "/auth/gmail/start": gmail_routes.handle_gmail_connect_start,
    "/auth/gmail/callback": gmail_routes.handle_gmail_connect_callback,
    "/api/drive/status": drive_routes.handle_drive_status,
    "/auth/drive/start": drive_routes.handle_drive_connect_start,
    "/auth/drive/callback": drive_routes.handle_drive_connect_callback,
    "/api/ai/settings": admin_routes.handle_ai_settings,
    "/api/matters": matter_routes.handle_matter_list,
    "/api/matters/export": admin_routes.handle_matter_backup,
    "/api/signing-entities": entity_routes.handle_signing_entities,
    "/api/telemetry": admin_routes.handle_telemetry,
}

_PUBLIC_GET_EXACT_ROUTES = {
    "/login": auth_routes.handle_login_page,
    "/api/auth/status": auth_routes.handle_auth_status,
    "/auth/google/start": auth_routes.handle_google_start,
    "/auth/google/callback": auth_routes.handle_google_callback,
}

_POST_EXACT_ROUTES = {
    "/api/review": _handle_text_review_post,
    "/api/review/ai-draft-validation": _handle_ai_draft_validation_post,
    "/api/review/ai-second-opinion": _handle_ai_second_opinion_post,
    "/api/review-document": _handle_document_review_post,
    "/api/matters": _handle_matter_upload_post,
    "/api/generate-nda": generation_routes.handle_generate_nda,
    "/api/fill-suggestions": fill_routes.handle_fill_suggestions,
    "/api/dashboard/search-intent": dashboard_routes.handle_dashboard_search_intent,
    "/api/send-document": _handle_send_document_post,
    "/api/gmail/import": gmail_routes.handle_gmail_import,
    "/api/gmail/send-redline": gmail_routes.handle_gmail_send_redline,
    "/api/gmail/settings": gmail_routes.handle_gmail_settings_update,
    "/api/gmail/disconnect": gmail_routes.handle_gmail_disconnect,
    "/api/drive/disconnect": drive_routes.handle_drive_disconnect,
    "/api/drive/upload-matter": drive_routes.handle_drive_upload_matter,
    "/api/admin/drive-settings": drive_routes.handle_drive_settings_update,
    "/api/ai/api-key": admin_routes.handle_ai_api_key_update,
    "/api/ai/settings": admin_routes.handle_ai_settings_update,
    "/api/demo/reset": matter_routes.handle_demo_reset,
    "/api/export-review-docx": review_routes.handle_review_docx_export,
    "/api/export-annotated-pdf": review_routes.handle_annotated_pdf_export,
    "/api/playbook": _handle_playbook_save_post,
    "/api/playbook/draft": _handle_playbook_draft_save_post,
    "/api/playbook/validate-draft": _handle_playbook_validate_draft_post,
    "/api/playbook/discard-draft": _handle_playbook_draft_discard_post,
    "/api/playbook/publish": _handle_playbook_publish_post,
    "/api/playbook/restore": _handle_playbook_restore_post,
}

_PUBLIC_POST_EXACT_ROUTES = {
    "/api/auth/logout": auth_routes.handle_logout,
}

_DELETE_EXACT_ROUTES = {
    "/api/ai/api-key": admin_routes.handle_ai_api_key_clear,
}


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
        if not self._authorize_host(send_body=send_body):
            return
        public_handler = _PUBLIC_GET_EXACT_ROUTES.get(path)
        if public_handler is not None:
            public_handler(self, send_body=send_body)
            return
        # Static assets (CSS/JS/fonts/logo) are the app shell, identical for every
        # user and shipped to every authenticated client anyway. Serve them before
        # the auth gate so the PUBLIC login page can load its own logo and font;
        # otherwise unauthenticated asset requests 302-redirect to /login.
        if path.startswith("/static/"):
            requested = (STATIC_DIR / path.removeprefix("/static/")).resolve()
            if STATIC_DIR not in requested.parents or not requested.is_file():
                self._send_json({"error": "Not found"}, status=404, send_body=send_body)
                return
            self._send_file(requested, send_body=send_body)
            return
        if not self._authorize_request(send_body=send_body, path=path):
            return
        if not self._rate_limit_request("GET", path, send_body=send_body):
            return
        handler = _GET_EXACT_ROUTES.get(path)
        if handler is not None:
            handler(self, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/review"):
            matter_routes.handle_matter_review(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/render-status"):
            matter_routes.handle_matter_render_status(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/render-pdf"):
            matter_routes.handle_matter_render_pdf(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and "/render-page/" in path:
            matter_routes.handle_matter_render_page(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/source"):
            matter_routes.handle_matter_source(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/reviewed-docx"):
            approval_routes.handle_matter_reviewed_docx(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/pdf-annotations"):
            pdf_markup_routes.handle_pdf_annotations_list(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/marked-up-pdf"):
            pdf_markup_routes.handle_marked_up_pdf(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/"):
            matter_routes.handle_matter_detail(self, path, send_body=send_body)
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
            self._send_download_file(requested, requested.name, DOCX_MIME, send_body=send_body)
            return
        self._send_json({"error": "Not found"}, status=404, send_body=send_body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authorize_host():
            return
        if not self._authorize_csrf("POST"):
            return
        public_handler = _PUBLIC_POST_EXACT_ROUTES.get(path)
        if public_handler is not None:
            public_handler(self)
            return
        if not self._authorize_request(path=path):
            return
        if not self._rate_limit_request("POST", path):
            return
        try:
            handler = _POST_EXACT_ROUTES.get(path)
            if handler is not None:
                handler(self)
                return
            if path.startswith("/api/matters/") and path.endswith("/stage"):
                matter_routes.handle_matter_stage_update(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/review-refresh"):
                matter_routes.handle_matter_review_refresh(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/reviewed"):
                matter_routes.handle_matter_reviewed_update(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/ai-first-review"):
                matter_routes.handle_matter_ai_first_review(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/summary"):
                matter_routes.handle_matter_summary(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/redline-draft"):
                matter_routes.handle_matter_redline_draft_update(self, path)
                return
            if path.startswith("/api/matters/") and "/clauses/" in path and path.endswith("/decision"):
                approval_routes.handle_clause_decision(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/approve"):
                approval_routes.handle_matter_approve(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/pdf-annotations"):
                pdf_markup_routes.handle_pdf_annotation_create(self, path)
                return
            self._send_json({"error": "Not found"}, status=404)
        except PlaybookTemplateError:
            self._send_playbook_template_error()
        except EvidenceProvenanceError:
            self._send_json({"error": "Clause evidence provenance drift detected."}, status=500)
        except matter_store.MatterStoreError as error:
            self._send_json({"error": str(error)}, status=500)
        except app_settings.AppSettingsError as error:
            self._send_json({"error": str(error)}, status=500)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if not self._authorize_host():
            return
        if not self._authorize_csrf("DELETE"):
            return
        if not self._authorize_request(path=path):
            return
        try:
            handler = _DELETE_EXACT_ROUTES.get(path)
            if handler is not None:
                handler(self)
                return
            if path.startswith("/api/matters/") and "/pdf-annotations/" in path:
                pdf_markup_routes.handle_pdf_annotation_delete(self, path)
                return
            if path.startswith("/api/matters/"):
                matter_routes.handle_matter_delete(self, path)
                return
            self._send_json({"error": "Not found"}, status=404)
        except matter_store.MatterStoreError as error:
            self._send_json({"error": str(error)}, status=500)

    def _read_json_payload(self) -> dict | None:
        content_length = self._read_content_length()
        if content_length is None:
            return None
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}", parse_constant=_reject_non_finite_json_constant)
        except (json.JSONDecodeError, RecursionError, ValueError):
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

    def _send_download_file(
        self,
        path: Path,
        filename: str,
        content_type: str,
        headers: dict[str, str] | None = None,
        *,
        send_body: bool = True,
    ) -> None:
        try:
            size = path.stat().st_size
            data = path.read_bytes() if send_body else b""
        except OSError:
            self._send_json({"error": "Export file could not be read."}, status=500, send_body=send_body)
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(size))
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
        if status >= 500:
            telemetry.increment("http_5xx_responses")
        elif status >= 400:
            telemetry.increment("http_4xx_responses")
        data = json.dumps(payload, indent=2).encode("utf-8") if send_body else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for header, value in (headers or {}).items():
            self.send_header(header, value)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _send_html(
        self,
        html: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        *,
        send_body: bool = True,
    ) -> None:
        data = html.encode("utf-8") if send_body else b""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for header, value in (headers or {}).items():
            self.send_header(header, value)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _send_redirect(
        self,
        location: str,
        *,
        status: int = 302,
        headers: dict[str, str] | None = None,
        send_body: bool = True,
    ) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        for header, value in (headers or {}).items():
            self.send_header(header, value)
        self.end_headers()

    def _send_playbook_template_error(self) -> None:
        self._send_json({"error": PLAYBOOK_TEMPLATE_ERROR_MESSAGE}, status=500)

    def _authorize_request(self, *, send_body: bool = True, path: str = "") -> bool:
        self.current_user_id = ""
        self.current_user = None
        if not _auth_required_for_host(str(self.server.server_address[0])):
            return True
        session_user = auth_routes.current_session_user(self)
        if session_user is not None:
            self.current_user = user_store.public_user(session_user)
            self.current_user_id = str(session_user.get("id") or "")
            return True
        if not _auth_method_configured():
            self._send_json({"error": AUTH_NOT_CONFIGURED_MESSAGE}, status=503, send_body=send_body)
            return False
        username = os.environ.get("NDA_AUTH_USERNAME", "").strip()
        password = os.environ.get("NDA_AUTH_PASSWORD", "")
        auth_header = self.headers.get("Authorization", "")
        if username and password and _basic_auth_matches(auth_header, username, password):
            credentials = _basic_auth_credentials(auth_header)
            self.current_user_id = credentials[0] if credentials else username
            self.current_user = {
                "id": self.current_user_id,
                "provider": "basic",
                "email": self.current_user_id,
                "name": self.current_user_id,
                "picture": "",
            }
            return True
        if _google_oauth_configured() and not path.startswith("/api/"):
            self._send_redirect("/login", send_body=send_body)
            return False
        headers = {"WWW-Authenticate": f'Basic realm="{AUTH_REALM}", charset="UTF-8"'} if _basic_auth_configured() else {}
        self._send_json(
            {"error": AUTH_REQUIRED_MESSAGE, "login_url": "/login" if _google_oauth_configured() else ""},
            status=401,
            headers=headers,
            send_body=send_body,
        )
        return False

    def _authorize_host(self, *, send_body: bool = True) -> bool:
        if host_header_allowed(self.headers.get("Host", ""), str(self.server.server_address[0])):
            return True
        telemetry.increment("host_header_rejections")
        self._send_json({"error": HOST_NOT_ALLOWED_MESSAGE}, status=403, send_body=send_body)
        return False

    def _authorize_csrf(self, method: str, *, send_body: bool = True) -> bool:
        if origin_allowed_for_request(
            method=method,
            origin_header=self.headers.get("Origin", ""),
            referer_header=self.headers.get("Referer", ""),
            host_header=self.headers.get("Host", ""),
            bind_host=str(self.server.server_address[0]),
        ):
            return True
        telemetry.increment("csrf_rejections")
        self._send_json({"error": CSRF_REJECTED_MESSAGE}, status=403, send_body=send_body)
        return False

    def _rate_limit_request(self, method: str, path: str, *, send_body: bool = True) -> bool:
        client_key = _rate_limit_client_key(
            self.client_address[0] if self.client_address else "unknown",
            self.headers.get("X-Forwarded-For", ""),
            getattr(self, "current_user_id", ""),
        )
        retry_after = _rate_limit_retry_after(method, path, client_key)
        if retry_after <= 0:
            return True
        telemetry.increment("rate_limit_hits")
        self._send_json(
            {"error": RATE_LIMITED_MESSAGE},
            status=429,
            headers={"Retry-After": str(retry_after)},
            send_body=send_body,
        )
        return False


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value is not allowed: {value}.")


def _log_background_error(message: str, error: Exception) -> None:
    print(f"{message}: {error.__class__.__name__}")


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
        sleep_seconds = MAX_GMAIL_SYNC_IDLE_SECONDS
        try:
            last_run, last_frequency, sleep_seconds = _gmail_sync_scheduler_step(last_run, last_frequency)
        except Exception as error:  # pragma: no cover - defensive background logging.
            _log_background_error("Gmail sync scheduler failed", error)
        time.sleep(sleep_seconds)


def _gmail_sync_scheduler_step(last_run: float, last_frequency: str) -> tuple[float, str, int]:
    settings = app_settings.gmail_settings()
    frequency = str(settings.get("sync_frequency") or app_settings.DEFAULT_GMAIL_SETTINGS["sync_frequency"])
    interval_seconds = app_settings.gmail_sync_interval_seconds(frequency)
    sleep_seconds = _gmail_sync_scheduler_sleep_seconds(interval_seconds)
    if frequency != last_frequency:
        last_run = 0.0
        last_frequency = frequency
    if settings.get("inbound_enabled", True):
        now = time.monotonic()
        if last_run and now - last_run < interval_seconds:
            return last_run, last_frequency, _gmail_sync_scheduler_remaining_sleep_seconds(
                last_run,
                now,
                interval_seconds,
            )
        if _gmail_sync_backoff_active():
            return last_run, last_frequency, sleep_seconds
        if not _gmail_inbound_configured_for_scheduled_sync():
            return now, last_frequency, sleep_seconds
        with _gmail_sync_process_lock() as lock_acquired:
            if lock_acquired:
                try:
                    _run_scheduled_gmail_sync()
                finally:
                    last_run = now
            else:
                last_run = now
    return last_run, last_frequency, sleep_seconds


def _gmail_inbound_configured_for_scheduled_sync() -> bool:
    if gmail_integration.gmail_sync_owner_user_ids():
        return True
    return not gmail_integration.gmail_role_setup_error("inbound")


def _gmail_sync_backoff_active() -> bool:
    return _GMAIL_SYNC_BACKOFF_UNTIL > time.time()


def _set_gmail_sync_backoff(error: Exception) -> None:
    global _GMAIL_SYNC_BACKOFF_UNTIL
    if not isinstance(error, gmail_integration.GmailRateLimitError):
        return
    retry_after = float(error.retry_after_epoch or 0.0)
    if retry_after <= time.time():
        retry_after = time.time() + 10 * 60
    _GMAIL_SYNC_BACKOFF_UNTIL = max(_GMAIL_SYNC_BACKOFF_UNTIL, retry_after)
    telemetry.increment("gmail_sync_rate_limit_failures")


def _clear_gmail_sync_backoff_for_tests() -> None:
    global _GMAIL_SYNC_BACKOFF_UNTIL
    _GMAIL_SYNC_BACKOFF_UNTIL = 0.0


def _gmail_sync_scheduler_sleep_seconds(interval_seconds: float) -> int:
    return max(1, math.ceil(interval_seconds))


def _gmail_sync_scheduler_remaining_sleep_seconds(last_run: float, now: float, interval_seconds: int) -> int:
    elapsed = max(0.0, now - last_run)
    remaining = max(1.0, interval_seconds - elapsed)
    return _gmail_sync_scheduler_sleep_seconds(remaining)


def _run_scheduled_gmail_sync() -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    telemetry.increment("gmail_sync_runs")
    try:
        owner_user_ids = gmail_integration.gmail_sync_owner_user_ids()
        if owner_user_ids:
            result = _run_scheduled_user_gmail_sync(owner_user_ids)
        else:
            result = gmail_integration.import_inbound_matters(limit=gmail_integration.MAX_GMAIL_IMPORT_LIMIT)
            result = {**result, "deduplicated_count": matter_store.deduplicate_gmail_matters()}
        finished_at = datetime.now(timezone.utc).isoformat()
        app_settings.record_gmail_sync(result, synced_at=finished_at, started_at=started_at, finished_at=finished_at)
        telemetry.increment("gmail_sync_successes")
    except Exception as error:  # pragma: no cover - defensive background logging.
        telemetry.increment("gmail_sync_failures")
        _set_gmail_sync_backoff(error)
        finished_at = datetime.now(timezone.utc).isoformat()
        app_settings.record_gmail_sync_error(
            str(error),
            started_at=started_at,
            finished_at=finished_at,
            query=gmail_integration._default_inbound_query(),
        )
        _log_background_error("Gmail scheduled sync failed", error)
        time.sleep(5)


def _run_scheduled_user_gmail_sync(owner_user_ids: list[str]) -> dict[str, object]:
    imported: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    accounts: list[str] = []
    queries: list[str] = []
    per_user: list[dict[str, object]] = []
    deduplicated_count = 0

    for owner_user_id in owner_user_ids:
        user_started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = gmail_integration.import_inbound_matters(
                limit=gmail_integration.MAX_GMAIL_IMPORT_LIMIT,
                owner_user_id=owner_user_id,
            )
            owner_deduplicated_count = matter_store.deduplicate_gmail_matters(owner_user_id=owner_user_id)
            result = {**result, "deduplicated_count": owner_deduplicated_count}
            user_finished_at = datetime.now(timezone.utc).isoformat()
            user_store.record_user_gmail_sync(
                owner_user_id,
                result,
                synced_at=user_finished_at,
                started_at=user_started_at,
                finished_at=user_finished_at,
            )
        except gmail_integration.GmailRateLimitError as error:
            user_finished_at = datetime.now(timezone.utc).isoformat()
            user_store.record_user_gmail_sync_error(
                owner_user_id,
                str(error),
                started_at=user_started_at,
                finished_at=user_finished_at,
                query=gmail_integration._default_inbound_query(),
            )
            raise
        except gmail_integration.GmailIntegrationError as error:
            user_finished_at = datetime.now(timezone.utc).isoformat()
            user_store.record_user_gmail_sync_error(
                owner_user_id,
                str(error),
                started_at=user_started_at,
                finished_at=user_finished_at,
                query=gmail_integration._default_inbound_query(),
            )
            skipped.append({
                "owner_user_id": owner_user_id,
                "reason": "user_sync_failed",
                "detail": str(error),
            })
            per_user.append({
                "owner_user_id": owner_user_id,
                "account": "",
                "imported_count": 0,
                "skipped_count": 1,
                "deduplicated_count": 0,
                "error": str(error),
            })
            continue

        result_imported = result.get("imported") if isinstance(result.get("imported"), list) else []
        result_skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
        account = str(result.get("account") or "")
        query = str(result.get("query") or "")
        if account and account not in accounts:
            accounts.append(account)
        if query and query not in queries:
            queries.append(query)
        imported.extend(item for item in result_imported if isinstance(item, dict))
        skipped.extend(item for item in result_skipped if isinstance(item, dict))
        deduplicated_count += owner_deduplicated_count
        per_user.append({
            "owner_user_id": owner_user_id,
            "account": account,
            "imported_count": len(result_imported),
            "skipped_count": len(result_skipped),
            "deduplicated_count": owner_deduplicated_count,
        })

    return {
        "account": ", ".join(accounts),
        "accounts": accounts,
        "deduplicated_count": deduplicated_count,
        "imported": imported,
        "per_user": per_user,
        "query": queries[0] if len(queries) == 1 else "per-user Gmail sync",
        "skipped": skipped,
    }


if __name__ == "__main__":
    main()
