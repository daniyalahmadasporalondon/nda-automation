"""Routes for the DocuSign send-for-signature flow (REAL OAuth + eSignature).

Endpoints (all owner-scoped from the AUTHENTICATED request — never a client body):

* ``GET  /api/docusign/status``                  — connection state (connected /
  configured / production / account label).
* ``POST /api/docusign/connect``                 — start REAL DocuSign OAuth
  (Authorization Code Grant) and return the consent URL to redirect to.
* ``GET  /auth/docusign/callback``               — OAuth callback: exchange the
  code for tokens, resolve account id + base URI, store the token.
* ``POST /api/docusign/disconnect``              — remove the user's DocuSign token.
* ``POST /api/matters/<id>/send-for-signature``  — create + send a real envelope.
* ``GET  /api/matters/<id>/signature-status``    — current envelope status (syncs).
* ``GET  /api/matters/<id>/signed-document``     — download the executed PDF.
* ``POST /api/docusign/webhook``                 — DocuSign Connect callback;
  verifies the HMAC signature when a key is configured; completes -> signed.

Auth/CSRF/Origin/host/rate-limit are enforced centrally in ``server.do_POST`` /
``do_GET`` before dispatch, exactly like the sibling Gmail/Drive routes. The
webhook is the one PUBLIC endpoint (DocuSign's servers call it with no session),
gated instead by the Connect HMAC signature.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from urllib.parse import parse_qs, quote, urlparse

from .. import (
    docusign_connection,
    docusign_integration,
    docusign_workflow,
    google_connection,
    matter_view,
    telemetry,
    user_store,
)
from ..matter_repository import DiskMatterRepository, MatterRepository
from .common import parse_matter_id, request_owner_user_id

logger = logging.getLogger(__name__)

DOCUSIGN_CONNECT_START_URL = "/api/docusign/connect"
# DocuSign Connect HMAC signature header. DocuSign sends one header per configured
# HMAC key, numbered from 1; we verify against the first.
HMAC_SIGNATURE_HEADER = "X-DocuSign-Signature-1"


def _repository(handler) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    return repository if repository is not None else DiskMatterRepository()


def _google_owner_user_id(handler) -> str:
    """The signed-in Google user id (DocuSign tokens are keyed per app user)."""
    return google_connection.connected_owner_user_id(
        getattr(handler, "current_user", None),
        owner_user_id=request_owner_user_id(handler),
    )


def _request_host(handler) -> str:
    """The server bind host, used to detect no-login (loopback) mode. Defensive."""
    server = getattr(handler, "server", None)
    address = getattr(server, "server_address", None)
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    return ""


def _docusign_owner_user_id(handler) -> str:
    """The owner DocuSign tokens are stored/read under for this request.

    Identical to :func:`request_owner_user_id` for every authenticated request.
    In no-login (loopback) mode the request owner is "" — which matter access
    treats as the single-tenant wildcard but token storage cannot key on — so this
    substitutes a stable local-dev owner so the OAuth connect persists and
    ``connected:true`` works. See ``docusign_connection.resolve_owner_user_id``.
    """
    return docusign_connection.resolve_owner_user_id(
        request_owner_user_id(handler), host=_request_host(handler)
    )


# ---------------------------------------------------------------------------
# Connection: status / connect / callback / disconnect
# ---------------------------------------------------------------------------
def handle_docusign_status(handler, *, send_body: bool = True) -> None:
    owner_user_id = _docusign_owner_user_id(handler)
    status = docusign_integration.connection_status(owner_user_id=owner_user_id)
    status["signed_in"] = bool(owner_user_id)
    status["connect_url"] = DOCUSIGN_CONNECT_START_URL
    if not status.get("configured"):
        status["needs_config"] = True
        status["config_message"] = (
            "DocuSign OAuth is not configured. Set NDA_DOCUSIGN_CLIENT_ID and "
            "NDA_DOCUSIGN_CLIENT_SECRET (and NDA_DOCUSIGN_OAUTH_REDIRECT_URI), then restart."
        )
    handler._send_json(status, send_body=send_body)


def handle_docusign_connect_start(handler, *, send_body: bool = True) -> None:
    """GET /api/docusign/connect — 302-redirect the browser to DocuSign consent.

    This is the PRIMARY connect path the frontend navigates to: the admin button
    sets ``window.location.href`` to ``status.connect_url`` (``/api/docusign/connect``
    with ``?next=`` appended), exactly like the Google connect button hits
    ``GET /auth/google/start``. So this must be a GET that 302s to the DocuSign
    consent URL (mirrors :func:`routes.auth.handle_google_start`), not a JSON POST.

    Honors ``?next`` for where the callback returns the user. Auth/owner is
    enforced like every sibling endpoint (a signed-in user is required). When
    OAuth is not configured it redirects back to ``next`` with a clear error flag
    rather than 404-ing, so the admin sees a message instead of a dead end.
    """
    query = parse_qs(urlparse(handler.path).query)
    next_path = query.get("next", ["/"])[0] or "/"

    owner_user_id = _docusign_owner_user_id(handler)
    if not owner_user_id:
        handler._send_redirect(_error_next(next_path, "signin_required"), send_body=send_body)
        return
    if not docusign_connection.oauth_configured():
        handler._send_redirect(_error_next(next_path, "docusign_not_configured"), send_body=send_body)
        return
    try:
        authorization_url = _build_consent_url(handler, owner_user_id, next_path)
    except docusign_connection.DocuSignConnectionError:
        handler._send_redirect(_error_next(next_path, "docusign_connect_failed"), send_body=send_body)
        return
    handler._send_redirect(authorization_url, send_body=send_body)


def handle_docusign_connect(handler) -> None:
    """POST /api/docusign/connect — JSON fallback that returns the consent URL.

    Kept for any frontend that prefers a CSRF-protected POST + client-side
    redirect: it returns ``{authorization_url}`` (the SAME URL the GET path 302s
    to) instead of redirecting server-side. The GET handler above is the path the
    current frontend actually navigates to.
    """
    owner_user_id = _docusign_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in before connecting DocuSign."}, status=403)
        return
    if not docusign_connection.oauth_configured():
        handler._send_json(
            {
                "error": (
                    "DocuSign OAuth is not configured. Set NDA_DOCUSIGN_CLIENT_ID and "
                    "NDA_DOCUSIGN_CLIENT_SECRET, then restart the app."
                ),
                "needs_config": True,
            },
            status=409,
        )
        return

    payload = handler._read_json_payload()
    if payload is None:
        return
    next_path = str(payload.get("next") or "/")
    try:
        authorization_url = _build_consent_url(handler, owner_user_id, next_path)
    except docusign_connection.DocuSignConnectionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    handler._send_json({"authorization_url": authorization_url})


def _build_consent_url(handler, owner_user_id: str, next_path: str) -> str:
    """Construct the DocuSign authorization URL (shared by the GET + POST paths).

    Mints a single-use OAuth state carrying ``next_path`` (so the callback returns
    the user where they started) and builds the consent URL with the configured
    client id + redirect uri + scopes on the configured auth server.
    """
    state = user_store.create_oauth_state(
        purpose="docusign",
        user_id=owner_user_id,
        next_path=next_path,
        metadata={"role": "docusign"},
    )
    return docusign_connection.build_authorization_url(
        redirect_uri=_redirect_uri(handler),
        state=state,
        login_hint=google_connection.login_hint(getattr(handler, "current_user", None)),
    )


def handle_docusign_callback(handler, *, send_body: bool = True) -> None:
    """OAuth callback: exchange the code, resolve the account, store the token."""
    owner_user_id = _docusign_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in before connecting DocuSign."}, status=403, send_body=send_body)
        return
    query = parse_qs(urlparse(handler.path).query)
    if query.get("error"):
        handler._send_json({"error": "DocuSign connection was not completed."}, status=400, send_body=send_body)
        return
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    state_record = user_store.consume_oauth_state(state, purpose="docusign", user_id=owner_user_id)
    if not code or state_record is None:
        handler._send_json(
            {"error": "DocuSign connection state is invalid or expired."}, status=400, send_body=send_body
        )
        return
    try:
        token_response = docusign_connection.exchange_code_for_token(code, redirect_uri=_redirect_uri(handler))
        userinfo = docusign_connection.fetch_userinfo(str(token_response.get("access_token") or ""))
        account = docusign_connection.default_account(userinfo)
        docusign_connection.save_user_token(owner_user_id, token_response, account)
    except docusign_connection.DocuSignConnectionError as error:
        handler._send_json({"error": str(error)}, status=502, send_body=send_body)
        return
    telemetry.increment("docusign_connections")
    next_path = str(state_record.get("next_path") or "/")
    handler._send_redirect(next_path, headers={"X-DocuSign-Connected": "1"}, send_body=send_body)


def handle_docusign_disconnect(handler) -> None:
    owner_user_id = _docusign_owner_user_id(handler)
    if not owner_user_id:
        handler._send_json({"error": "Sign in before disconnecting DocuSign."}, status=403)
        return
    try:
        removed = docusign_connection.disconnect_user(owner_user_id)
    except docusign_connection.DocuSignConnectionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    handler._send_json({"disconnected": removed})


# ---------------------------------------------------------------------------
# Per-matter: send for signature / status / signed document
# ---------------------------------------------------------------------------
def handle_send_for_signature(handler, path: str) -> None:
    """POST /api/matters/<id>/send-for-signature — create + send a real envelope.

    Body: ``{signers?: [{name,email,anchor?,role?}], signing_order?: parallel|sequential}``.
    Owner from the authenticated request. A missing/owner-mismatched matter -> 404
    no-op. A disconnected DocuSign -> 409 with ``needs_connect``.
    """
    matter_id = parse_matter_id(path, suffix="/send-for-signature")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    payload = handler._read_json_payload()
    if payload is None:
        return

    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    # #30 (defense-in-depth): refuse a SECOND send when the matter already carries
    # an ACTIVE, non-terminal envelope. The frontend disables the button after a
    # successful send, but a FE-only guard can be bypassed by a direct request, so
    # the route is the authoritative duplicate-send backstop. A terminal envelope
    # (completed/declined/voided) is NOT active — a fresh resend after a void is
    # legitimate and allowed.
    if _has_active_envelope(matter):
        telemetry.increment("docusign_send_duplicate_rejected")
        handler._send_json(_already_sent_payload(matter), status=409)
        return

    signers = payload.get("signers") if isinstance(payload.get("signers"), list) else None
    signing_order = str(payload.get("signing_order") or docusign_integration.DEFAULT_SIGNING_ORDER)
    email_subject = str(payload.get("email_subject") or "")

    telemetry.increment("docusign_send_requests")
    try:
        result = docusign_workflow.send_for_signature(
            matter,
            matter_id,
            owner_user_id,
            signers=signers,
            signing_order=signing_order,
            email_subject=email_subject,
            repository=repository,
        )
    except docusign_connection.DocuSignNotConnectedError:
        telemetry.increment("docusign_send_failed")
        handler._send_json(_needs_connect_payload(), status=409)
        return
    except docusign_workflow.NoSignableDocumentError as error:
        telemetry.increment("docusign_send_failed")
        handler._send_json({"error": str(error)}, status=409)
        return
    except docusign_workflow.SignerResolutionError as error:
        telemetry.increment("docusign_send_failed")
        handler._send_json({"error": str(error)}, status=400)
        return
    except docusign_integration.DocuSignError as error:
        telemetry.increment("docusign_send_failed")
        handler._send_json({"error": str(error)}, status=502)
        return
    except docusign_workflow.DocuSignWorkflowError as error:
        telemetry.increment("docusign_send_failed")
        handler._send_json({"error": str(error)}, status=502)
        return

    telemetry.increment("docusign_send_succeeded")
    handler._send_json(
        {
            "envelope_id": result.envelope_id,
            "status": result.status,
            "signers": result.signers,
            "document_filename": result.document_filename,
            "matter": matter_view.public_matter(result.matter),
        },
        status=201,
    )


def handle_signature_status(handler, path: str, *, send_body: bool = True) -> None:
    """GET /api/matters/<id>/signature-status — current envelope status (live sync)."""
    matter_id = parse_matter_id(path, suffix="/signature-status")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return

    try:
        result = docusign_workflow.sync_signature_status(
            matter,
            matter_id,
            owner_user_id,
            repository=repository,
            # #10: resolve the Drive-token owner the SAME way the deliberate
            # Save-to-Drive route does (the Google-scoped id, "" in no-login mode)
            # so the on-complete archive authenticates to Drive correctly instead of
            # mis-using the matter/request id.
            drive_token_owner_user_id=_google_owner_user_id(handler),
        )
    except docusign_connection.DocuSignNotConnectedError:
        handler._send_json(_needs_connect_payload(), status=409, send_body=send_body)
        return
    except docusign_workflow.DocuSignWorkflowError as error:
        # No envelope yet, or a transient sync error: surface the stored state.
        stored = matter.get(docusign_workflow.SIGNATURE_FIELD)
        handler._send_json(
            {
                "envelope_id": str(stored.get("envelope_id") or "") if isinstance(stored, dict) else "",
                "status": str(stored.get("status") or "") if isinstance(stored, dict) else "",
                "completed": False,
                "error": str(error),
            },
            status=200,
            send_body=send_body,
        )
        return

    handler._send_json(
        {
            "envelope_id": result.envelope_id,
            "status": result.status,
            "completed": result.completed,
            "signed_artifact_id": result.signed_artifact_id,
            "matter": matter_view.public_matter(result.matter),
        },
        send_body=send_body,
    )


def handle_signed_document(handler, path: str, *, send_body: bool = True) -> None:
    """GET /api/matters/<id>/signed-document — download the executed PDF.

    Serves the stored ``signed`` artifact bytes; if none is captured yet but the
    envelope is complete, it syncs first to capture it.
    """
    matter_id = parse_matter_id(path, suffix="/signed-document")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return

    pdf_bytes = _signed_artifact_bytes(matter, matter_id, owner_user_id, repository)
    if not pdf_bytes:
        # Try a live sync to capture a freshly-completed envelope's executed PDF.
        try:
            docusign_workflow.sync_signature_status(
                matter,
                matter_id,
                owner_user_id,
                repository=repository,
                drive_token_owner_user_id=_google_owner_user_id(handler),
            )
            refreshed = repository.get_matter(matter_id, owner_user_id=owner_user_id) or matter
            pdf_bytes = _signed_artifact_bytes(refreshed, matter_id, owner_user_id, repository)
        except docusign_workflow.DocuSignWorkflowError:
            pdf_bytes = b""

    if not pdf_bytes:
        handler._send_json(
            {"error": "No executed document is available for this matter yet."},
            status=404,
            send_body=send_body,
        )
        return

    filename = f"{matter_id}-executed.pdf"
    handler._send_bytes(pdf_bytes, filename=filename, content_type="application/pdf", send_body=send_body)


# ---------------------------------------------------------------------------
# Webhook (DocuSign Connect) — PUBLIC, HMAC-verified
# ---------------------------------------------------------------------------
def handle_docusign_webhook(handler) -> None:
    """DocuSign Connect callback. PUBLIC; verified by the Connect HMAC signature.

    Reads the RAW body (needed for HMAC), verifies ``X-DocuSign-Signature-1``
    against ``NDA_DOCUSIGN_CONNECT_HMAC_KEY`` when configured, parses the envelope
    id + status, finds the owning matter, and on ``completed`` runs the workflow
    sync to capture the signed artifact + flip the matter to executed.

    Fail-safe: a malformed/unsignable payload returns 400 without touching any
    matter; an unknown envelope returns 200 (ack so DocuSign stops retrying).
    """
    telemetry.increment("docusign_webhook_requests")
    raw_body = _read_raw_body(handler)
    if raw_body is None:
        return

    if not _verify_hmac(handler, raw_body):
        telemetry.increment("docusign_webhook_rejected")
        handler._send_json({"error": "Invalid DocuSign webhook signature."}, status=401)
        return

    envelope_id, status = _parse_webhook(raw_body)
    if not envelope_id:
        handler._send_json({"error": "DocuSign webhook payload was not understood."}, status=400)
        return

    located = _find_matter_by_envelope(handler, envelope_id)
    if located is None:
        # Unknown envelope — ack so DocuSign stops retrying, but do nothing.
        handler._send_json({"received": True, "matched": False})
        return
    matter, matter_id, owner_user_id = located

    completed = False
    try:
        # #10 (webhook path): there is NO session/handler to read the Google-scoped
        # owner from, so we leave drive_token_owner_user_id as None. The archiver
        # then resolves the Drive-token owner from the matched matter's connected
        # Google account (drive_integration.drive_token_owner_for_matter): the matter
        # owner's per-user Drive token when present, else the server-global token
        # ("" — the no-login / local-demo path). This is the correct identity for
        # the on-complete archive instead of threading the raw matter owner id into
        # the Drive layer.
        result = docusign_workflow.sync_signature_status(
            matter, matter_id, owner_user_id, repository=_repository(handler)
        )
        completed = result.completed
    except docusign_workflow.DocuSignWorkflowError:
        # Best-effort: never fail the webhook ack on a transient sync error.
        completed = str(status or "").lower() == docusign_integration.STATUS_COMPLETED
    telemetry.increment("docusign_webhook_processed")
    handler._send_json({"received": True, "matched": True, "completed": completed})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _needs_connect_payload() -> dict:
    return {
        "error": "DocuSign is not connected.",
        "needs_connect": True,
        "connect_url": DOCUSIGN_CONNECT_START_URL,
    }


def _has_active_envelope(matter) -> bool:
    """True when the matter already has a sent, non-terminal DocuSign envelope.

    An envelope is "active" once it has an ``envelope_id`` AND its status is not
    one of the terminal states (completed/declined/voided). A matter with no
    envelope, or one whose envelope has reached a terminal state, is free to send
    (a resend after a void/decline is legitimate). Defensive against the field
    being absent or a non-dict.
    """
    signature = matter.get(docusign_workflow.SIGNATURE_FIELD) if isinstance(matter, dict) else None
    if not isinstance(signature, dict):
        return False
    if not str(signature.get("envelope_id") or "").strip():
        return False
    status = str(signature.get("status") or "").strip().lower()
    return status not in docusign_integration.TERMINAL_STATUSES


def _already_sent_payload(matter) -> dict:
    """409 body for a duplicate send: the matter is already out for signature."""
    signature = matter.get(docusign_workflow.SIGNATURE_FIELD) if isinstance(matter, dict) else {}
    envelope_id = str(signature.get("envelope_id") or "") if isinstance(signature, dict) else ""
    status = str(signature.get("status") or "") if isinstance(signature, dict) else ""
    return {
        "error": "This NDA has already been sent for signature.",
        "already_sent": True,
        "envelope_id": envelope_id,
        "status": status,
    }


def _redirect_uri(handler) -> str:
    configured = docusign_connection.configured_redirect_uri()
    if configured:
        return configured
    return f"{google_connection.request_base_url(handler)}/auth/docusign/callback"


def _error_next(next_path: str, error_code: str) -> str:
    """Return a SAME-ORIGIN relative path appended with ``?docusign_error=<code>``.

    Used by the GET connect-start path to bounce the user back to where they came
    from with a clear error flag instead of a 404. ``next_path`` is forced to a
    safe relative path (leading ``/``, no scheme/host) so it can never become an
    open redirect to an external site; anything unsafe falls back to ``/``.
    """
    safe = str(next_path or "/")
    # Reject absolute URLs, scheme-relative URLs, and anything not rooted at "/".
    if not safe.startswith("/") or safe.startswith("//"):
        safe = "/"
    separator = "&" if "?" in safe else "?"
    return f"{safe}{separator}docusign_error={quote(error_code, safe='')}"


def _signed_artifact_bytes(matter, matter_id, owner_user_id, repository) -> bytes:
    from .. import artifact_service
    from ..artifact_registry import ROLE_SIGNED, latest_artifact_for_role

    signed = latest_artifact_for_role(matter, ROLE_SIGNED)
    if signed is None:
        return b""
    data = artifact_service.get_artifact_bytes(
        matter_id, signed.id, repository=repository, owner_user_id=owner_user_id
    )
    return data or b""


def _read_raw_body(handler) -> bytes | None:
    """Read the raw request body for HMAC verification (sends 400 on bad length)."""
    content_length = handler._read_content_length()
    if content_length is None:
        return None
    try:
        return handler.rfile.read(content_length) if content_length else b""
    except OSError:
        handler._send_json({"error": "Could not read DocuSign webhook body."}, status=400)
        return None


def _verify_hmac(handler, raw_body: bytes) -> bool:
    """Verify the Connect HMAC signature (fail-CLOSED when a key is configured).

    DocuSign computes base64(HMAC-SHA256(rawBody, secret)) and sends it in
    ``X-DocuSign-Signature-1``.

    * When ``NDA_DOCUSIGN_CONNECT_HMAC_KEY`` IS set we REQUIRE a matching
      signature: a missing or mismatched signature returns ``False`` so the caller
      rejects with 401 and touches no matter (anti-spoof, finding #22).
    * When NO key is configured we cannot verify, so the webhook is effectively
      unauthenticated. We still let the request proceed (so an unconfigured
      demo/sandbox keeps working) but log a LOUD WARNING every time — silently
      accepting an unauthenticated state-changing callback is the bug we are
      fixing. Set the key in production to enforce authentication.

    Uses the constant-time :func:`hmac.compare_digest` for the comparison.
    """
    key = docusign_connection.connect_hmac_key()
    if not key:
        logger.warning(
            "DocuSign Connect webhook is UNAUTHENTICATED: %s is not set, so the "
            "/api/docusign/webhook callback accepts any caller and cannot verify "
            "that a completion event really came from DocuSign. Set %s (and the "
            "matching HMAC key in DocuSign Connect) to enforce authentication.",
            docusign_connection.CONNECT_HMAC_KEY_ENV,
            docusign_connection.CONNECT_HMAC_KEY_ENV,
        )
        return True
    provided = handler.headers.get(HMAC_SIGNATURE_HEADER, "")
    if not provided:
        logger.warning(
            "Rejected DocuSign Connect webhook: %s is configured but the request "
            "carried no %s signature header.",
            docusign_connection.CONNECT_HMAC_KEY_ENV,
            HMAC_SIGNATURE_HEADER,
        )
        return False
    import base64

    digest = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    valid = hmac.compare_digest(expected, provided.strip())
    if not valid:
        logger.warning(
            "Rejected DocuSign Connect webhook: %s signature did not match the "
            "configured %s.",
            HMAC_SIGNATURE_HEADER,
            docusign_connection.CONNECT_HMAC_KEY_ENV,
        )
    return valid


def _parse_webhook(raw_body: bytes) -> tuple[str, str]:
    """Extract ``(envelope_id, status)`` from a Connect payload (JSON or XML)."""
    import json

    text = raw_body.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _parse_webhook_xml(text)
    if not isinstance(payload, dict):
        return "", ""
    # Connect 2.0 (JSON) shape: {"event": "...", "data": {"envelopeId": "...",
    # "envelopeSummary": {"status": "..."}}}. Also accept flatter shapes.
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    envelope_id = str(
        data.get("envelopeId")
        or data.get("envelope_id")
        or payload.get("envelopeId")
        or ""
    ).strip()
    summary = data.get("envelopeSummary") if isinstance(data.get("envelopeSummary"), dict) else {}
    status = str(summary.get("status") or data.get("status") or payload.get("status") or "").strip().lower()
    return envelope_id, status


def _parse_webhook_xml(text: str) -> tuple[str, str]:
    """Extract from the legacy Connect XML payload (best-effort, dependency-free)."""
    import re

    envelope_match = re.search(r"<EnvelopeID>([^<]+)</EnvelopeID>", text, re.IGNORECASE)
    status_match = re.search(r"<Status>([^<]+)</Status>", text, re.IGNORECASE)
    envelope_id = envelope_match.group(1).strip() if envelope_match else ""
    status = status_match.group(1).strip().lower() if status_match else ""
    return envelope_id, status


def _find_matter_by_envelope(handler, envelope_id: str):
    """Locate ``(matter, matter_id, owner_user_id)`` whose stored envelope matches.

    The webhook is unauthenticated, so we scan all matters for the one carrying
    this envelope id under ``matter["docusign"]["envelope_id"]``. The owner is read
    from the matched matter (never from the request).
    """
    repository = _repository(handler)
    try:
        matters = repository.list_matters()
    except TypeError:
        matters = repository.list_matters(owner_user_id="")
    for matter in matters or []:
        if not isinstance(matter, dict):
            continue
        signature = matter.get(docusign_workflow.SIGNATURE_FIELD)
        if isinstance(signature, dict) and str(signature.get("envelope_id") or "") == envelope_id:
            return matter, str(matter.get("id") or ""), str(matter.get("owner_user_id") or "")
    return None


