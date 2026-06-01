from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import app_settings, export_service, gmail_integration, matter_store, redline_export_service as redline_export_service, telemetry
from .checker import PLAYBOOK_PATH, PlaybookTemplateError, review_nda
from .deployment import (
    DURABLE_DATA_DIR_REQUIRED_MESSAGE as DURABLE_DATA_DIR_REQUIRED_MESSAGE,
    EPHEMERAL_DATA_DIR_MESSAGE as EPHEMERAL_DATA_DIR_MESSAGE,
    EPHEMERAL_EXPORTS_DIR_MESSAGE as EPHEMERAL_EXPORTS_DIR_MESSAGE,
    _deployment_status_for_host as _deployment_status_for_host,
    _is_ephemeral_storage_path as _is_ephemeral_storage_path,
    _validate_public_auth,
    _validate_public_storage,
)
from .docx_export import DOCX_MIME
from .http_auth import (
    AUTH_NOT_CONFIGURED_MESSAGE,
    AUTH_REALM,
    AUTH_REQUIRED_MESSAGE,
    _auth_required_for_host,
    _basic_auth_matches,
    _env_flag_enabled as _env_flag_enabled,
    _is_loopback_host as _is_loopback_host,
)
from .rate_limit import (
    DEFAULT_RATE_LIMIT_PER_MINUTE as DEFAULT_RATE_LIMIT_PER_MINUTE,
    RATE_LIMITED_MESSAGE,
    _rate_limit_bucket_name as _rate_limit_bucket_name,
    _rate_limit_per_window as _rate_limit_per_window,
    _rate_limit_retry_after,
    _rate_limit_window_seconds as _rate_limit_window_seconds,
    _reset_rate_limits as _reset_rate_limits,
)
from .ingestion_service import create_matter_from_document, extract_document
from .routes import admin as admin_routes
from .routes import gmail as gmail_routes
from .routes import matters as matter_routes
from .routes import playbook as playbook_routes
from .routes import review as review_routes
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


def _parse_matter_id(path: str, *, suffix: str = "") -> str | None:
    return _route_parse_matter_id(path, suffix=suffix)


def _handle_index_get(handler, *, send_body: bool) -> None:
    handler._send_file(STATIC_DIR / "index.html", send_body=send_body)


def _handle_playbook_get(handler, *, send_body: bool) -> None:
    handler._send_file(PLAYBOOK_PATH, "application/json", send_body=send_body)


def _handle_text_review_post(handler) -> None:
    review_routes.handle_text_review(handler, review_nda_func=review_nda)


def _handle_document_review_post(handler) -> None:
    review_routes.handle_document_review(
        handler,
        extract_document_func=extract_document,
        review_nda_func=review_nda,
    )


def _handle_matter_upload_post(handler) -> None:
    matter_routes.handle_matter_upload(
        handler,
        create_matter_from_document_func=create_matter_from_document,
    )


def _handle_playbook_save_post(handler) -> None:
    playbook_routes.handle_playbook_save(
        handler,
        playbook_path=PLAYBOOK_PATH,
        replace_file=os.replace,
    )


_GET_EXACT_ROUTES = {
    "/": _handle_index_get,
    "/api/deployment/status": admin_routes.handle_deployment_status,
    "/playbook": _handle_playbook_get,
    "/api/gmail/status": gmail_routes.handle_gmail_status,
    "/api/matters": matter_routes.handle_matter_list,
    "/api/matters/export": admin_routes.handle_matter_backup,
    "/api/telemetry": admin_routes.handle_telemetry,
}

_POST_EXACT_ROUTES = {
    "/api/review": _handle_text_review_post,
    "/api/review-document": _handle_document_review_post,
    "/api/matters": _handle_matter_upload_post,
    "/api/gmail/import": gmail_routes.handle_gmail_import,
    "/api/gmail/send-redline": gmail_routes.handle_gmail_send_redline,
    "/api/gmail/settings": gmail_routes.handle_gmail_settings_update,
    "/api/demo/reset": matter_routes.handle_demo_reset,
    "/api/export-review-docx": review_routes.handle_review_docx_export,
    "/api/playbook": _handle_playbook_save_post,
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
        if not self._authorize_request(send_body=send_body):
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
        if path.startswith("/api/matters/"):
            matter_routes.handle_matter_detail(self, path, send_body=send_body)
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
            if path.startswith("/api/matters/") and path.endswith("/redline-draft"):
                matter_routes.handle_matter_redline_draft_update(self, path)
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
        if status >= 500:
            telemetry.increment("http_5xx_responses")
        elif status >= 400:
            telemetry.increment("http_4xx_responses")
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

    def _rate_limit_request(self, method: str, path: str, *, send_body: bool = True) -> bool:
        retry_after = _rate_limit_retry_after(
            method,
            path,
            self.client_address[0] if self.client_address else "unknown",
        )
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
    if frequency != last_frequency:
        last_run = 0.0
        last_frequency = frequency
    if settings.get("inbound_enabled", True):
        now = time.monotonic()
        if now - last_run >= interval_seconds:
            with _gmail_sync_process_lock() as lock_acquired:
                if lock_acquired:
                    try:
                        _run_scheduled_gmail_sync()
                    finally:
                        last_run = now
                else:
                    last_run = now
    return last_run, last_frequency, _gmail_sync_scheduler_sleep_seconds(interval_seconds)


def _gmail_sync_scheduler_sleep_seconds(interval_seconds: int) -> int:
    return min(max(1, interval_seconds), MAX_GMAIL_SYNC_IDLE_SECONDS)


def _run_scheduled_gmail_sync() -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    telemetry.increment("gmail_sync_runs")
    try:
        result = gmail_integration.import_inbound_matters(limit=gmail_integration.MAX_GMAIL_IMPORT_LIMIT)
        result = {**result, "deduplicated_count": matter_store.deduplicate_gmail_matters()}
        finished_at = datetime.now(timezone.utc).isoformat()
        app_settings.record_gmail_sync(result, synced_at=finished_at, started_at=started_at, finished_at=finished_at)
        telemetry.increment("gmail_sync_successes")
    except Exception as error:  # pragma: no cover - defensive background logging.
        telemetry.increment("gmail_sync_failures")
        finished_at = datetime.now(timezone.utc).isoformat()
        app_settings.record_gmail_sync_error(
            str(error),
            started_at=started_at,
            finished_at=finished_at,
            query=gmail_integration.DEFAULT_INBOUND_QUERY,
        )
        _log_background_error("Gmail scheduled sync failed", error)
        time.sleep(5)


if __name__ == "__main__":
    main()
