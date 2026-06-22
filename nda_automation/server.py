from __future__ import annotations

import argparse
import hashlib
import json
import logging
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

from . import (
    app_settings,
    entity_registry,
    export_service,
    gmail_integration,
    matter_store,
    pdf_export_service,
    redline_export_service as redline_export_service,
    telemetry,
    user_store,
)
from .playbook_runtime import read_playbook_from_path
from .checker import (
    PLAYBOOK_PATH,
    ai_validate_draft_fix,
    EvidenceProvenanceError,
    PlaybookTemplateError,
    ai_second_opinion_for_clause,
)
from .deployment import (
    DATA_DIR_NOT_PERSISTED as DATA_DIR_NOT_PERSISTED,
    DURABLE_DATA_DIR_REQUIRED_MESSAGE as DURABLE_DATA_DIR_REQUIRED_MESSAGE,
    EPHEMERAL_DATA_DIR_MESSAGE as EPHEMERAL_DATA_DIR_MESSAGE,
    EPHEMERAL_EXPORTS_DIR_MESSAGE as EPHEMERAL_EXPORTS_DIR_MESSAGE,
    EPHEMERAL_USERS_PATH_MESSAGE as EPHEMERAL_USERS_PATH_MESSAGE,
    NON_PERSISTENT_DATA_DIR_WARNING as NON_PERSISTENT_DATA_DIR_WARNING,
    _deployment_status_for_host as _deployment_status_for_host,
    _is_ephemeral_storage_path as _is_ephemeral_storage_path,
    _validate_public_auth,
    _validate_public_storage,
    record_data_dir_boot,
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
from .ingestion_service import create_matter_from_document
from . import lifecycle_counter, lifecycle_signed
from .matter_repository import DiskMatterRepository
from .review_engine import review_nda_with_active_engine
from .routes import admin as admin_routes
from .routes import approval as approval_routes
from .routes import auth as auth_routes
from .routes import corpus as corpus_routes
from .routes import dashboard as dashboard_routes
from .routes import docusign as docusign_routes
from .routes import drive as drive_routes
from .routes import entities as entity_routes
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

logger = logging.getLogger(__name__)

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


def _handle_reassess_clause_post(handler) -> None:
    review_routes.handle_reassess_clause(handler)


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


def _handle_playbook_suggest_wording_post(handler, path: str) -> None:
    playbook_routes.handle_playbook_suggest_wording(
        handler,
        path,
        playbook_path=PLAYBOOK_PATH,
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
    # Admin-only Drive folder browser (powers the "Browse Drive" picker in the
    # admin Drive settings panel). Admin-gated inside the handler, like the
    # /api/admin/drive-settings update route.
    "/api/admin/drive-folders": drive_routes.handle_drive_folders,
    "/api/docusign/status": docusign_routes.handle_docusign_status,
    # GET /api/docusign/connect 302-redirects the browser to DocuSign consent (the
    # primary connect path the admin button navigates to, mirroring
    # /auth/google/start). The POST variant in _POST_EXACT_ROUTES is a JSON fallback.
    "/api/docusign/connect": docusign_routes.handle_docusign_connect_start,
    "/api/pdf-export/status": lambda handler, *, send_body=True: handler._send_json(
        {"pdf_export": pdf_export_service.converter_health()},
        send_body=send_body,
    ),
    "/auth/drive/start": drive_routes.handle_drive_connect_start,
    "/auth/drive/callback": drive_routes.handle_drive_connect_callback,
    "/auth/docusign/callback": docusign_routes.handle_docusign_callback,
    "/api/admin/personalisation-settings": admin_routes.handle_personalisation_settings,
    # Non-admin self-serve: the caller reads/writes their OWN personalisation
    # (scoped to their owner-user-id inside the handler), distinct from the
    # admin-only global default above.
    "/api/me/personalisation-settings": admin_routes.handle_my_personalisation_settings,
    "/api/admin/admins": admin_routes.handle_admin_list,
    "/api/ai/availability": admin_routes.handle_ai_availability,
    "/api/ai/settings": admin_routes.handle_ai_settings,
    "/api/matters": matter_routes.handle_matter_list,
    "/api/matters/export": admin_routes.handle_matter_backup,
    "/api/corpus": corpus_routes.handle_corpus,
    "/api/signing-entities": entity_routes.handle_signing_entities,
    "/api/dashboard/search-config": dashboard_routes.handle_dashboard_search_config,
    # Admin-only Entities console workspace (live registry + playbook law
    # options). Admin-gated inside the handler, like the other admin GET routes.
    "/api/admin/signing-entities": entity_routes.handle_admin_signing_entities,
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
    "/api/review/reassess-clause": _handle_reassess_clause_post,
    "/api/matters": _handle_matter_upload_post,
    "/api/generate-nda": generation_routes.handle_generate_nda,
    "/api/dashboard/assistant": dashboard_routes.handle_dashboard_assistant,
    "/api/dashboard/search-intent": dashboard_routes.handle_dashboard_search_intent,
    "/api/send-document": _handle_send_document_post,
    "/api/gmail/import": gmail_routes.handle_gmail_import,
    "/api/gmail/send-redline": gmail_routes.handle_gmail_send_redline,
    "/api/gmail/settings": gmail_routes.handle_gmail_settings_update,
    "/api/gmail/disconnect": gmail_routes.handle_gmail_disconnect,
    "/api/drive/disconnect": drive_routes.handle_drive_disconnect,
    "/api/drive/upload-matter": drive_routes.handle_drive_upload_matter,
    "/api/docusign/connect": docusign_routes.handle_docusign_connect,
    "/api/docusign/disconnect": docusign_routes.handle_docusign_disconnect,
    "/api/admin/drive-settings": drive_routes.handle_drive_settings_update,
    # POST /api/admin/drive-folders creates a new folder under ``parent`` (the
    # write counterpart of the GET browse route above). Admin-gated inside the
    # handler, like the other drive-admin routes.
    "/api/admin/drive-folders": drive_routes.handle_drive_create_folder,
    # Entities console writes (admin-gated inside the handler; CSRF enforced by
    # do_POST before dispatch). The save body is the full replacement registry.
    "/api/admin/signing-entities": entity_routes.handle_admin_signing_entities_save,
    "/api/admin/signing-entities/validate": entity_routes.handle_admin_signing_entities_validate,
    "/api/admin/personalisation-settings": admin_routes.handle_personalisation_settings_update,
    # Non-admin self-serve write counterpart (strict per-owner isolation).
    "/api/me/personalisation-settings": admin_routes.handle_my_personalisation_settings_update,
    "/api/admin/admins/add": admin_routes.handle_admin_add,
    "/api/ai/api-key": admin_routes.handle_ai_api_key_update,
    "/api/ai/settings": admin_routes.handle_ai_settings_update,
    "/api/demo/reset": matter_routes.handle_demo_reset,
    "/api/export-review-docx": review_routes.handle_review_docx_export,
    "/api/playbook": _handle_playbook_save_post,
    "/api/playbook/draft": _handle_playbook_draft_save_post,
    "/api/playbook/validate-draft": _handle_playbook_validate_draft_post,
    "/api/playbook/discard-draft": _handle_playbook_draft_discard_post,
    "/api/playbook/publish": _handle_playbook_publish_post,
    "/api/playbook/restore": _handle_playbook_restore_post,
}

_PUBLIC_POST_EXACT_ROUTES = {
    "/api/auth/logout": auth_routes.handle_logout,
    "/api/auth/logout-all": auth_routes.handle_logout_all,
}

_DELETE_EXACT_ROUTES = {
    "/api/ai/api-key": admin_routes.handle_ai_api_key_clear,
    # Mutating + CSRF-protected (do_DELETE enforces CSRF, same as POST). The body
    # carries {email}; the handler is admin-gated like the other admin routes.
    "/api/admin/admins": admin_routes.handle_admin_remove,
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
        if path.startswith("/api/matters/") and path.endswith("/source-pdf"):
            matter_routes.handle_matter_source_pdf(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/source-docx"):
            matter_routes.handle_matter_source_docx(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and "/render-page/" in path:
            matter_routes.handle_matter_render_page(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/source"):
            matter_routes.handle_matter_source(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/signature-status"):
            docusign_routes.handle_signature_status(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/signed-document"):
            docusign_routes.handle_signed_document(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/reviewed-docx"):
            approval_routes.handle_matter_reviewed_docx(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/reviewed-pdf"):
            approval_routes.handle_matter_reviewed_pdf(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/pdf-annotations"):
            pdf_markup_routes.handle_pdf_annotations_list(self, path, send_body=send_body)
            return
        if path.startswith("/api/matters/") and path.endswith("/marked-up-pdf"):
            pdf_markup_routes.handle_marked_up_pdf(self, path, send_body=send_body)
            return
        if path.startswith("/api/corpus/artifacts/"):
            corpus_routes.handle_corpus_artifact_download(self, self.path, send_body=send_body)
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
        # The DocuSign Connect webhook is a server-to-server callback (no browser
        # session, no Origin header) authenticated by its HMAC signature, not by
        # CSRF/session. It must therefore bypass the CSRF + session gates, but is
        # still host-checked above and rate-limited here. The handler verifies the
        # HMAC signature before touching any matter.
        if path == "/api/docusign/webhook":
            if not self._rate_limit_request("POST", path):
                return
            try:
                docusign_routes.handle_docusign_webhook(self)
            except matter_store.MatterStoreError as error:
                logger.warning("DocuSign webhook persistence failed: %s", error)
                self._send_json({"error": matter_store.friendly_matter_store_message(error)}, status=500)
            except app_settings.AppSettingsError as error:
                self._send_json({"error": str(error)}, status=500)
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
            if path.startswith("/api/playbook/clause/") and path.endswith("/suggest-wording"):
                _handle_playbook_suggest_wording_post(self, path)
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
            if path.startswith("/api/matters/") and path.endswith("/summary"):
                matter_routes.handle_matter_summary(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/redline-draft"):
                matter_routes.handle_matter_redline_draft_update(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/counterparty"):
                matter_routes.handle_matter_counterparty_confirm(self, path)
                return
            if path.startswith("/api/matters/") and "/clauses/" in path and path.endswith("/decision"):
                approval_routes.handle_clause_decision(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/approve"):
                approval_routes.handle_matter_approve(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/send-for-signature"):
                docusign_routes.handle_send_for_signature(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/signed"):
                lifecycle_signed.handle_signed_upload(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/mark-executed"):
                lifecycle_signed.handle_mark_executed(self, path)
                return
            if path.startswith("/api/matters/") and path.endswith("/counter"):
                lifecycle_counter.handle_counter_upload(self, path)
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
            logger.warning("Matter store failure (POST %s): %s", path, error)
            self._send_json({"error": matter_store.friendly_matter_store_message(error)}, status=500)
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
            logger.warning("Matter store failure (DELETE %s): %s", path, error)
            self._send_json({"error": matter_store.friendly_matter_store_message(error)}, status=500)
        except app_settings.AppSettingsError as error:
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

    def _send_bytes(
        self,
        data: bytes,
        *,
        filename: str = "",
        content_type: str | None = None,
        send_body: bool = True,
    ) -> None:
        etag = f'"sha256-{hashlib.sha256(data).hexdigest()}"'
        detected_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
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

    _record_data_dir_boot_sentinel()
    _migrate_entity_signatory_fills()
    _validate_entity_registry_against_playbook()
    _reconcile_interrupted_reviews()

    server = ThreadingHTTPServer((args.host, args.port), NdaAutomationHandler)
    _start_gmail_sync_scheduler()
    print(f"nda-automation running at http://{args.host}:{args.port}")
    server.serve_forever()


def _migrate_entity_signatory_fills() -> None:
    """One-time fill of named entities' signatories in the persisted registry at boot.

    The seed (``DEFAULT_SIGNING_ENTITIES``) ships GENERIC
    ``[Authorised Signatory]`` / ``[Title]`` placeholders so signatories stay
    ordinary, editable registry data rather than hardcoded code defaults. The real
    initial signers live as DATA in the migration mapping
    (``entity_store._SIGNATORY_FILL_BY_ID``). This boot step applies that mapping
    to the LIVE persistent registry ONCE: for each named entity whose signatory is
    still the exact placeholder or empty, it sets the mapped name/title -- and ONLY
    then. A real, admin-entered signatory is never overwritten (so editing one in
    the Entities console and saving makes it permanent; a later run leaves it), the
    fill is field-level and idempotent, and entities not in the mapping are
    untouched. Fail-safe: a store/disk error is swallowed so it can never crash
    boot. Runs BEFORE the drift check so the filled values are what gets validated.
    """

    try:
        from . import entity_store  # noqa: PLC0415 - deferred; mirrors entity_registry.

        filled = entity_store.migrate_signatory_fills()
        if filled:
            noun = "entity" if filled == 1 else "entities"
            print(f"Entity signatory migration: filled {filled} {noun}.")
    except Exception as error:  # pragma: no cover - defensive: never crash boot.
        _log_background_error("Entity signatory migration failed", error)


def _validate_entity_registry_against_playbook() -> None:
    """Proactively catch entity-registry <-> playbook drift at boot. WARN-only.

    The signing-entity registry joins onto the live playbook by governing-law
    option id, and its per-entity ``jurisdiction`` must reconcile (at bucket
    granularity) with the playbook's per-option ``forum_jurisdiction``. If the
    playbook drifts -- a renamed/removed approved option, a cached label gone stale,
    or a law/forum jurisdiction divergence -- a generated NDA could carry the wrong
    governing law or a court from the wrong jurisdiction.

    This runs at startup so the drift is surfaced in the boot log proactively,
    rather than only when someone hits generate. Detection only -- it NEVER refuses
    to boot (avoid prod-down); generation still hard-fails on an actually-unapproved
    option downstream.
    """

    try:
        playbook = read_playbook_from_path(PLAYBOOK_PATH)
    except Exception as error:  # pragma: no cover - missing/unreadable playbook.
        _log_background_error("Entity-registry drift check could not load the playbook", error)
        return
    try:
        entity_registry.validate_registry_against_playbook(playbook)
    except ValueError as drift:
        print(f"WARNING: entity-registry/playbook drift detected at boot: {drift}")
    except Exception as error:  # pragma: no cover - defensive: never crash boot.
        _log_background_error("Entity-registry drift check failed", error)


def _reconcile_interrupted_reviews() -> None:
    """Heal reviews orphaned by a restart at boot, BEFORE the server serves.

    A review stamps ``review_status="in_progress"`` before handing off to the
    background pool; if the process died mid-flight that stamp is the last durable
    state and the record sits ``in_progress`` forever. This runs the PURE status
    reconcile (``ingestion_service.reconcile_interrupted_reviews``) once at boot:
    matters already carrying a full ai_first result heal to ``completed``, the rest
    are stamped the durable, recoverable ``interrupted`` status the FE renders as a
    calm Retry. It NEVER enqueues a job or calls the AI -- re-running is the user's
    on-demand Refresh -- so it cannot produce a review storm.

    Fail-safe like its boot neighbours: any error (including an unpersisted disk so
    there is nothing to reconcile) is logged and swallowed -- it must never crash
    boot.
    """

    try:
        from .ingestion_service import reconcile_interrupted_reviews  # noqa: PLC0415

        summary = reconcile_interrupted_reviews()
        if summary.get("interrupted") or summary.get("completed"):
            print(
                "Interrupted-review reconcile: "
                f"interrupted={summary.get('interrupted', 0)} "
                f"completed={summary.get('completed', 0)}."
            )
    except Exception as error:  # pragma: no cover - defensive: never crash boot.
        _log_background_error("Interrupted-review reconcile failed", error)


def _record_data_dir_boot_sentinel() -> None:
    """Prove NDA_DATA_DIR durability at boot and WARN loudly if it isn't persisting.

    Detection only -- never refuses to boot (avoid prod-down).  An UNMOUNTED
    /var/data passes the path-string ephemeral denylist silently; the boot sentinel
    is the cross-platform proof that something actually survived a restart.
    """
    try:
        verdict = record_data_dir_boot(matter_store.DATA_DIR)
    except Exception as error:  # pragma: no cover - defensive: detection must not crash boot.
        _log_background_error("Data dir boot-sentinel check failed", error)
        return
    if verdict == DATA_DIR_NOT_PERSISTED:
        print(f"WARNING: {NON_PERSISTENT_DATA_DIR_WARNING}")


def _start_gmail_sync_scheduler() -> None:
    # Inbound NDAs are intentionally NOT auto-reviewed (they import "Not Reviewed"
    # and are reviewed only on-demand), so there is no startup recovery sweep to
    # re-enqueue them -- that sweep was the Gmail-storm re-enqueue engine and is
    # removed. The scheduler just polls Gmail for new inbound matters.
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


# Render-level EMERGENCY kill switch for the scheduled Gmail sync. This works even
# when the admin UI / settings store is unreachable, so it is the absolute override
# and is checked BEFORE the settings are read. It is complementary to the
# `sync_enabled` admin setting (the normal pause/resume control): set
# NDA_GMAIL_SYNC_ENABLED to a falsey value (false/0/no/off) to hard-stop polling.
GMAIL_SYNC_ENABLED_ENV = "NDA_GMAIL_SYNC_ENABLED"


def _gmail_sync_enabled() -> bool:
    raw = os.environ.get(GMAIL_SYNC_ENABLED_ENV, "").strip().lower()
    return not raw or raw not in {"false", "0", "no", "off"}


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
    # Env-level emergency kill switch FIRST, before touching settings, so it stops
    # polling even if the settings store is unreachable. Reset last_run + idle short
    # so flipping it back on resumes promptly (no up-to-a-full-cadence wait).
    if not _gmail_sync_enabled():
        telemetry.increment("gmail_sync_skipped_disabled")
        return 0.0, last_frequency, MAX_GMAIL_SYNC_IDLE_SECONDS
    settings = app_settings.gmail_settings()
    frequency = str(settings.get("sync_frequency") or app_settings.DEFAULT_GMAIL_SETTINGS["sync_frequency"])
    interval_seconds = app_settings.gmail_sync_interval_seconds(frequency)
    sleep_seconds = _gmail_sync_scheduler_sleep_seconds(interval_seconds)
    if frequency != last_frequency:
        last_run = 0.0
        last_frequency = frequency
    # Master pause gate (admin setting): when an admin pauses the sync, skip the
    # WHOLE scheduled step (no poll, no inbound work). This is the real stop that
    # "Disconnect Gmail" used to fake -- the scheduler obeys it. Checked before
    # inbound_enabled, which remains the narrower inbound-only gate. Reset last_run
    # and idle short so resuming the toggle polls promptly rather than waiting up to
    # a full cadence (10 min / 2 h).
    if not app_settings.gmail_scheduler_enabled(settings):
        telemetry.increment("gmail_sync_skipped_disabled")
        return 0.0, last_frequency, MAX_GMAIL_SYNC_IDLE_SECONDS
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


# Opt-in flag for the legacy server-level inbound token fallback. When NO user
# has connected Gmail (e.g. the last account just disconnected), the scheduler
# used to silently fall back to the env/local server token and keep polling --
# so "Disconnect Gmail" did not stop the inbound sync, which blindsided users.
# The fallback is now opt-in: set NDA_GMAIL_SERVER_INBOUND=1 to run a
# deliberately-configured server-token inbound deployment. Default off ⇒ no
# connected account ⇒ the scheduled sync does not run.
GMAIL_SERVER_INBOUND_ENV = "NDA_GMAIL_SERVER_INBOUND"


def _gmail_server_inbound_fallback_enabled() -> bool:
    return _env_flag_enabled(GMAIL_SERVER_INBOUND_ENV)


def _gmail_inbound_configured_for_scheduled_sync() -> bool:
    if gmail_integration.gmail_sync_owner_user_ids():
        return True
    # No connected user. Only honor the server-level/env token fallback when an
    # operator has explicitly opted in; otherwise a disconnected account leaves
    # the scheduler idle instead of storming a leftover server token.
    if not _gmail_server_inbound_fallback_enabled():
        return False
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
    # The admin-configurable per-poll limit (setting, falling back to the
    # NDA_GMAIL_IMPORT_LIMIT env default), re-clamped to the safe ceiling. One
    # effective limit for every import_inbound_matters call in this sync.
    import_limit = app_settings.gmail_import_limit()
    try:
        owner_user_ids = gmail_integration.gmail_sync_owner_user_ids()
        if owner_user_ids:
            result = _run_scheduled_user_gmail_sync(owner_user_ids, import_limit=import_limit)
        elif not _gmail_server_inbound_fallback_enabled():
            # Defense-in-depth: never touch a leftover server/env token from the
            # scheduled path unless the operator explicitly opted in. With no
            # connected user this makes the poll a no-op (zero import/review
            # calls) instead of storming a disconnected mailbox.
            telemetry.increment("gmail_sync_skipped_no_connected_user")
            return
        else:
            result = gmail_integration.import_inbound_matters(limit=import_limit)
            result = {**result, "deduplicated_count": DiskMatterRepository().deduplicate_gmail_matters()}
            # No inbound auto-review recovery sweep: inbound NDAs import "Not
            # Reviewed" and are reviewed only on-demand, so there is nothing to
            # re-enqueue here. (The sweep was the storm re-enqueue engine.)
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


def _run_scheduled_user_gmail_sync(
    owner_user_ids: list[str],
    *,
    import_limit: int | None = None,
) -> dict[str, object]:
    imported: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    accounts: list[str] = []
    queries: list[str] = []
    per_user: list[dict[str, object]] = []
    deduplicated_count = 0
    # Resolve the effective per-poll limit when not supplied by the caller (e.g. a
    # direct/legacy call): setting first, env default as fallback.
    effective_import_limit = (
        import_limit if import_limit is not None else app_settings.gmail_import_limit()
    )

    for owner_user_id in owner_user_ids:
        user_started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = gmail_integration.import_inbound_matters(
                limit=effective_import_limit,
                owner_user_id=owner_user_id,
            )
            owner_deduplicated_count = DiskMatterRepository().deduplicate_gmail_matters(
                owner_user_id=owner_user_id
            )
            result = {**result, "deduplicated_count": owner_deduplicated_count}
            # No inbound auto-review recovery sweep (inbound NDAs are reviewed
            # on-demand only; the sweep was the storm re-enqueue engine).
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
