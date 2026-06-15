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
from urllib.parse import parse_qs, urlparse

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


# ---------------------------------------------------------------------------
# Connection: status / connect / callback / disconnect
# ---------------------------------------------------------------------------
def handle_docusign_status(handler, *, send_body: bool = True) -> None:
    owner_user_id = request_owner_user_id(handler)
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


def handle_docusign_connect(handler) -> None:
    """Start the REAL DocuSign OAuth consent flow; return the authorization URL.

    The browser POSTs here (CSRF-protected), receives ``{authorization_url}``, and
    redirects the user to DocuSign to grant consent. The callback below completes
    the token exchange.
    """
    owner_user_id = request_owner_user_id(handler)
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
        state = user_store.create_oauth_state(
            purpose="docusign",
            user_id=owner_user_id,
            next_path=next_path,
            metadata={"role": "docusign"},
        )
        authorization_url = docusign_connection.build_authorization_url(
            redirect_uri=_redirect_uri(handler),
            state=state,
            login_hint=google_connection.login_hint(getattr(handler, "current_user", None)),
        )
    except docusign_connection.DocuSignConnectionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    handler._send_json({"authorization_url": authorization_url})


def handle_docusign_callback(handler, *, send_body: bool = True) -> None:
    """OAuth callback: exchange the code, resolve the account, store the token."""
    owner_user_id = request_owner_user_id(handler)
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
    owner_user_id = request_owner_user_id(handler)
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
            matter, matter_id, owner_user_id, repository=repository
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
            docusign_workflow.sync_signature_status(matter, matter_id, owner_user_id, repository=repository)
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


def _redirect_uri(handler) -> str:
    configured = docusign_connection.configured_redirect_uri()
    if configured:
        return configured
    return f"{google_connection.request_base_url(handler)}/auth/docusign/callback"


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
    """Verify the Connect HMAC signature; True when valid OR no key is configured.

    DocuSign computes base64(HMAC-SHA256(rawBody, secret)) and sends it in
    ``X-DocuSign-Signature-1``. When no key is configured we cannot verify, so we
    accept (the operator chose not to enable HMAC); when a key IS configured we
    require a matching signature.
    """
    key = docusign_connection.connect_hmac_key()
    if not key:
        return True
    provided = handler.headers.get(HMAC_SIGNATURE_HEADER, "")
    if not provided:
        return False
    import base64

    digest = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, provided.strip())


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


