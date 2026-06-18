"""DocuSign eSignature integration — send a finalized NDA for signature.

This is the REAL, operating e-signature client. The running app always uses the
live DocuSign eSignature REST API: a user clicks "Connect DocuSign", grants
consent (Authorization Code Grant, in :mod:`docusign_connection`), and from then
on envelopes, status polls, the executed-PDF download and voids all hit the
DocuSign REST API with that user's authorized token.

Layout:

* :class:`DocuSignClient` — the interface the workflow drives:
  ``create_envelope`` / ``get_envelope_status`` / ``download_completed`` /
  ``void_envelope``.
* :class:`HttpDocuSignClient` — the REAL eSignature REST client. It is the ONLY
  client the factory returns to the running app. It builds the envelope-create
  call (base64 document + recipients + ``signHere``/``dateSigned`` anchor tabs),
  the status GET, the combined (executed) PDF GET, and the void PUT. It authorizes
  via :mod:`docusign_connection` (per-user OAuth token, refreshed on expiry).
* :class:`FakeDocuSignClient` — a TEST DOUBLE for automated unit tests ONLY. It
  is clearly separated and is NEVER returned by :func:`get_client`; the running
  app never touches it. Tests inject it explicitly.

Factory: :func:`get_client` returns an :class:`HttpDocuSignClient` bound to the
signed-in user's real DocuSign account, or raises
:class:`DocuSignNotConnectedError` when the user has not connected DocuSign.
There is NO demo fallback in the product path.

Required configuration so "click Connect" works — see :mod:`docusign_connection`:
NDA_DOCUSIGN_CLIENT_ID, NDA_DOCUSIGN_CLIENT_SECRET, NDA_DOCUSIGN_OAUTH_REDIRECT_URI,
NDA_DOCUSIGN_AUTH_SERVER (demo|production), NDA_DOCUSIGN_CONNECT_HMAC_KEY.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from . import docusign_connection

# ---------------------------------------------------------------------------
# Status ladder (DocuSign's envelope status vocabulary)
# ---------------------------------------------------------------------------
STATUS_CREATED = "created"
STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_COMPLETED = "completed"
STATUS_DECLINED = "declined"
STATUS_VOIDED = "voided"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_DECLINED, STATUS_VOIDED})

# A per-recipient signed status. DocuSign reports each recipient's own status on
# the recipients endpoint ("created"/"sent"/"delivered"/"completed"/"declined").
# "completed" is that recipient HAVING SIGNED. We normalize the per-recipient
# vocabulary to this small set so the matter view can show a clean per-party
# state (signed / awaiting / declined) without leaking DocuSign's full status
# vocabulary to the UI.
RECIPIENT_SIGNED = "signed"
RECIPIENT_AWAITING = "awaiting"
RECIPIENT_DECLINED = "declined"

DEFAULT_EMAIL_SUBJECT = "Please sign: NDA"

# Signing-order modes. PARALLEL is the default: both recipients share the same
# routingOrder so either side can sign in any order (the user's decision —
# signing order does not matter). SEQUENTIAL routes recipients in turn.
SIGNING_ORDER_PARALLEL = "parallel"
SIGNING_ORDER_SEQUENTIAL = "sequential"
SIGNING_ORDERS = (SIGNING_ORDER_PARALLEL, SIGNING_ORDER_SEQUENTIAL)
DEFAULT_SIGNING_ORDER = SIGNING_ORDER_PARALLEL

# eSignature REST API version segment.
REST_API_VERSION = "v2.1"


class DocuSignError(RuntimeError):
    """A DocuSign operation could not be completed."""


# Re-export the connection-layer errors so callers can catch a single taxonomy.
DocuSignNotConnectedError = docusign_connection.DocuSignNotConnectedError


class DocuSignEnvelopeNotFoundError(DocuSignError):
    """The envelope id is unknown to DocuSign."""


@dataclass
class Signer:
    """One recipient on an envelope.

    ``routing_order`` drives signing order. Under ``parallel`` (default) every
    signer shares order 1 so either side can sign in any order; under
    ``sequential`` orders increase so DocuSign routes them in turn. ``anchor`` is
    the anchor string the signature/date tabs attach to in the document — for a
    generated NDA this is the distinct per-party token planted on that party's
    signature line (``nda_generation.SIGNATURE_ANCHOR_*``), so the field lands on
    the right line and the two parties never collide; when empty the tabs fall
    back to the signer name.
    """

    name: str
    email: str
    routing_order: int = 1
    role: str = ""
    anchor: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "email": self.email,
            "routing_order": self.routing_order,
            "role": self.role,
            "anchor": self.anchor,
        }


def normalize_signers(signers: Any, *, signing_order: str = DEFAULT_SIGNING_ORDER) -> list[Signer]:
    """Coerce a list of signer dicts/``Signer``s into ``Signer`` objects.

    Assigns routing orders from the signing-order mode when a signer carries no
    explicit one: ``parallel`` -> all 1 (any order); ``sequential`` -> 1,2,3...
    Rejects an empty list or any signer missing a name/email.
    """
    order = signing_order if signing_order in SIGNING_ORDERS else DEFAULT_SIGNING_ORDER
    result: list[Signer] = []
    for index, raw in enumerate(signers or [], start=1):
        if isinstance(raw, Signer):
            signer = raw
        elif isinstance(raw, dict):
            signer = Signer(
                name=str(raw.get("name") or "").strip(),
                email=str(raw.get("email") or "").strip(),
                routing_order=_coerce_routing_order(raw.get("routing_order")),
                role=str(raw.get("role") or "").strip(),
                anchor=str(raw.get("anchor") or "").strip(),
            )
        else:
            raise DocuSignError("Each signer must be a name/email object.")
        if not signer.name or not signer.email:
            raise DocuSignError("Each signer needs a name and an email address.")
        if not _coerce_routing_order(signer.routing_order):
            signer.routing_order = 1 if order == SIGNING_ORDER_PARALLEL else index
        elif order == SIGNING_ORDER_PARALLEL:
            # Parallel: collapse any per-signer order to 1 so neither side gates
            # the other (the request can still pass sequential to override).
            signer.routing_order = 1
        result.append(signer)
    if not result:
        raise DocuSignError("At least one signer is required to send for signature.")
    return result


def _coerce_routing_order(value: Any) -> int:
    try:
        order = int(value)
    except (TypeError, ValueError):
        return 0
    return order if order > 0 else 0


def normalize_recipient_status(status: Any) -> str:
    """Collapse a DocuSign per-recipient status to the small UI vocabulary.

    DocuSign's recipient status ladder is ``created -> sent -> delivered ->
    completed`` (a recipient is "completed" once they have SIGNED), plus terminal
    ``declined`` / ``autoresponded``. We map ``completed`` to
    :data:`RECIPIENT_SIGNED`, ``declined`` to :data:`RECIPIENT_DECLINED`, and
    everything else (still out for signature) to :data:`RECIPIENT_AWAITING`. An
    unknown / blank value fails toward ``awaiting`` (never "signed").
    """
    value = str(status or "").strip().casefold()
    if value == STATUS_COMPLETED:
        return RECIPIENT_SIGNED
    if value == STATUS_DECLINED:
        return RECIPIENT_DECLINED
    return RECIPIENT_AWAITING


def parse_envelope_recipients(payload: Any) -> list[dict[str, Any]]:
    """Project a DocuSign ``GET /envelopes/{id}/recipients`` body into a flat list.

    DocuSign returns recipients bucketed by type (``signers``, ``carbonCopies``,
    ...), each an array of recipient objects carrying ``email`` / ``name`` /
    ``status`` and (when signed) ``signedDateTime``. We only care about the
    ``signers`` bucket — those are the parties who actually sign — and reduce each
    to ``{email, name, status, signed_at}`` with the status normalized via
    :func:`normalize_recipient_status`. Pure + defensive: a non-dict body, a
    missing/odd ``signers`` bucket, or a non-dict recipient each degrade to an
    empty list / skipped entry rather than raising.
    """
    if not isinstance(payload, dict):
        return []
    signers = payload.get("signers")
    if not isinstance(signers, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in signers:
        if not isinstance(raw, dict):
            continue
        email = str(raw.get("email") or "").strip()
        if not email:
            continue
        result.append(
            {
                "email": email,
                "name": str(raw.get("name") or "").strip(),
                "status": normalize_recipient_status(raw.get("status")),
                "signed_at": str(raw.get("signedDateTime") or "").strip(),
            }
        )
    return result


class DocuSignClient(Protocol):
    """The four operations the send-for-signature workflow drives."""

    def create_envelope(
        self,
        document_bytes: bytes,
        filename: str,
        signers: list[Signer],
        *,
        signing_order: str = DEFAULT_SIGNING_ORDER,
        email_subject: str = DEFAULT_EMAIL_SUBJECT,
    ) -> dict[str, Any]: ...

    def get_envelope_status(self, envelope_id: str) -> str: ...

    def get_envelope_recipients(self, envelope_id: str) -> list[dict[str, Any]]: ...

    def download_completed(self, envelope_id: str) -> bytes: ...

    def void_envelope(self, envelope_id: str, reason: str) -> dict[str, Any]: ...


def build_envelope_definition(
    document_bytes: bytes,
    filename: str,
    signers: list[Signer],
    *,
    email_subject: str = DEFAULT_EMAIL_SUBJECT,
) -> dict[str, Any]:
    """The DocuSign envelope-create body: base64 document + tabbed recipients.

    Each recipient gets a ``signHere`` + ``dateSigned`` tab anchored to its
    ``anchor`` string so the signature lands at the right spot regardless of page
    layout. ``routingOrder`` carries the (parallel-by-default) signing order.
    ``status="sent"`` makes DocuSign dispatch the envelope immediately.

    Pure + dependency-free so it is unit-testable without any network.
    """
    import base64

    if not isinstance(document_bytes, (bytes, bytearray)) or not document_bytes:
        raise DocuSignError("No document bytes to send for signature.")
    document_b64 = base64.b64encode(bytes(document_bytes)).decode("ascii")
    recipient_signers = []
    for index, signer in enumerate(signers, start=1):
        recipient_signers.append(
            {
                "email": signer.email,
                "name": signer.name,
                "recipientId": str(index),
                "routingOrder": str(signer.routing_order or 1),
                "tabs": _tabs_for(signer),
            }
        )
    return {
        "emailSubject": str(email_subject or DEFAULT_EMAIL_SUBJECT),
        "documents": [
            {
                "documentBase64": document_b64,
                "name": str(filename or "document.pdf"),
                "fileExtension": _file_extension(filename),
                "documentId": "1",
            }
        ],
        "recipients": {"signers": recipient_signers},
        "status": STATUS_SENT,
    }


def _tabs_for(signer: Signer) -> dict[str, Any]:
    """The signHere + dateSigned anchor tabs for one signer.

    ``signer.anchor`` is the per-party token planted on that party's ``By:``
    signature line in a generated NDA (it falls back to the signer name only when
    no explicit anchor is set, e.g. a non-generated document). The token is planted
    as the FIRST (hidden) run of the ``By: ______`` line, so it resolves at the
    LEFT edge of that party's signature-table cell.

    DocuSign positioning rule: it places a tab so the LOWER-LEFT corner of the tab
    sits at the LOWER-RIGHT corner of the anchor text's bounding box, and the
    (~1in-wide) signHere tab then grows RIGHTWARD and UPWARD from there. Because the
    anchor is at the cell's LEFT edge:

    * the **signHere** tab is placed with a small POSITIVE X offset to clear the
      visible ``By:`` label and land on the blank underscores, and a small POSITIVE
      Y offset to sit on the line — growing rightward INTO the ~2.3in-wide cell,
      never off any page edge;
    * the **dateSigned** tab sits just BELOW the signature line (larger positive Y)
      at the same X, so it never collides with the signature and stays inside the
      cell.

    Two earlier builds both 400'd with ``INVALID_USER_OFFSET`` (HTTP 400):

    1. ``anchorXOffset = -180`` (pixels) with an end-of-line anchor pushed the tab
       ~2.5in LEFT, off the page's LEFT edge.
    2. A small POSITIVE X with the anchor still at the END of the line: the Aspora
       box is flush against the page's RIGHT margin, so the rightward-growing tab
       ran off the page's RIGHT edge (the clamp's non-negative invariant only ever
       guarded the left/top edges, never the right/bottom).

    Anchoring at the START of the line (see ``nda_generation._write_signature_cell``)
    is the structural fix: the tab now grows into the cell's blank space from the
    LEFT, so it can leave the page on neither edge for either party. ``_clamp_offset``
    bounds the offsets to a small on-page range as a backstop.

    ``ignoreIfNotPresent`` keeps a single missing anchor from failing the whole
    envelope create (DocuSign would otherwise 400 if the string is absent) — a
    defence-in-depth guard for the case a non-anchored document is ever sent with
    an anchor set; the field is simply not placed rather than the send blocked.
    """
    anchor = signer.anchor or signer.name
    return {
        "signHereTabs": [
            {
                "anchorString": anchor,
                "anchorUnits": "pixels",
                # Clear the visible "By:" label and land on the blank underscores,
                # on the signature line; grows rightward into the cell.
                "anchorXOffset": _clamp_offset("36"),
                "anchorYOffset": _clamp_offset("0"),
                "anchorIgnoreIfNotPresent": "true",
            }
        ],
        "dateSignedTabs": [
            {
                "anchorString": anchor,
                "anchorUnits": "pixels",
                # Drop the date onto the line just below the signature, same X so it
                # stays inside the cell.
                "anchorXOffset": _clamp_offset("36"),
                "anchorYOffset": _clamp_offset("20"),
                "anchorIgnoreIfNotPresent": "true",
            }
        ],
    }


# The widest a tab offset may be from its anchor, in the chosen ``anchorUnits``
# (pixels @ 72dpi here). The signature box is ~2.3in wide and the anchor sits at
# its LEFT edge, so this bound (~1in) keeps a ~1in-wide tab plus its offset well
# inside the cell and clear of the page's right margin.
_MAX_ANCHOR_OFFSET_PIXELS = 72  # ~1 inch


def _clamp_offset(value: Any) -> str:
    """Clamp an anchor offset to ``[0, _MAX_ANCHOR_OFFSET_PIXELS]`` (as a string).

    DocuSign rejects the whole envelope with ``INVALID_USER_OFFSET`` (HTTP 400) if
    an anchor offset drives a tab past a page boundary. The anchor is planted at
    the LEFT edge of the signature cell and the tab grows rightward/upward, so a
    small non-negative X/Y offset keeps the (~1in-wide) tab inside the ~2.3in cell
    and clear of every page edge — left/top (offset is non-negative) and
    right/bottom (offset is bounded well under the cell's remaining width). This
    guard makes a future bad value degrade gracefully (the tab is merely
    repositioned) instead of failing the send. Non-numeric input falls back to
    ``0``.
    """
    try:
        offset = int(round(float(value)))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, min(offset, _MAX_ANCHOR_OFFSET_PIXELS))
    return str(offset)


# ---------------------------------------------------------------------------
# Real HTTP client — the operating path
# ---------------------------------------------------------------------------
class HttpDocuSignClient:
    """The real DocuSign eSignature REST client (the running app's client).

    Bound to ONE user's authorized DocuSign account. Authorizes every call with a
    fresh bearer token from :mod:`docusign_connection` (refreshed on expiry).

    ``http`` is an injectable transport for unit tests; in production it is this
    module's ``urllib``-backed default, so the real app needs no extra HTTP
    dependency.
    """

    name = "http"

    def __init__(
        self,
        *,
        owner_user_id: str,
        account_id: str = "",
        base_uri: str = "",
        http: Any | None = None,
    ) -> None:
        self._owner_user_id = str(owner_user_id or "")
        self._account_id = str(account_id or "").strip()
        self._base_uri = str(base_uri or "").strip()
        self._http = http or _UrllibTransport()

    # --- interface ---------------------------------------------------------
    def create_envelope(
        self,
        document_bytes: bytes,
        filename: str,
        signers: list[Signer],
        *,
        signing_order: str = DEFAULT_SIGNING_ORDER,
        email_subject: str = DEFAULT_EMAIL_SUBJECT,
    ) -> dict[str, Any]:
        normalized = normalize_signers(signers, signing_order=signing_order)
        definition = build_envelope_definition(
            document_bytes, filename, normalized, email_subject=email_subject
        )
        response = self._request_json("POST", f"{self._account_path()}/envelopes", body=definition)
        envelope_id = str(response.get("envelopeId") or "")
        if not envelope_id:
            raise DocuSignError("DocuSign did not return an envelope id.")
        return {"envelope_id": envelope_id, "status": str(response.get("status") or STATUS_SENT)}

    def get_envelope_status(self, envelope_id: str) -> str:
        response = self._request_json("GET", f"{self._account_path()}/envelopes/{_segment(envelope_id)}")
        return str(response.get("status") or "")

    def get_envelope_recipients(self, envelope_id: str) -> list[dict[str, Any]]:
        # ``/recipients`` lists every recipient with their OWN status +
        # signedDateTime; we project just the ``signers`` bucket via the pure
        # parser so the per-party signed state is decoupled from the wire shape.
        response = self._request_json(
            "GET", f"{self._account_path()}/envelopes/{_segment(envelope_id)}/recipients"
        )
        return parse_envelope_recipients(response)

    def download_completed(self, envelope_id: str) -> bytes:
        # ``/documents/combined`` is the single merged PDF of every signed document
        # plus the certificate of completion.
        return self._request_bytes(
            "GET", f"{self._account_path()}/envelopes/{_segment(envelope_id)}/documents/combined"
        )

    def void_envelope(self, envelope_id: str, reason: str) -> dict[str, Any]:
        response = self._request_json(
            "PUT",
            f"{self._account_path()}/envelopes/{_segment(envelope_id)}",
            body={"status": STATUS_VOIDED, "voidedReason": str(reason or "Voided by sender.")},
        )
        return {
            "envelope_id": str(response.get("envelopeId") or envelope_id),
            "status": str(response.get("status") or STATUS_VOIDED),
        }

    # --- internals ---------------------------------------------------------
    def _account_path(self) -> str:
        if not self._account_id:
            raise DocuSignNotConnectedError("DocuSign account id is not resolved; reconnect DocuSign.")
        return f"/restapi/{REST_API_VERSION}/accounts/{_segment(self._account_id)}"

    def _url(self, path: str) -> str:
        if not self._base_uri:
            raise DocuSignNotConnectedError("DocuSign base URI is not resolved; reconnect DocuSign.")
        return f"{self._base_uri.rstrip('/')}{path}"

    def _auth_headers(self) -> dict[str, str]:
        token = docusign_connection.access_token_for_user(self._owner_user_id)
        return {"Authorization": f"Bearer {token}"}

    def _request_json(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = self._auth_headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        status_code, payload = self._http.request_json(method, self._url(path), headers=headers, json_body=body)
        self._raise_for_status(status_code, path=path, payload=payload)
        return payload if isinstance(payload, dict) else {}

    def _request_bytes(self, method: str, path: str) -> bytes:
        status_code, content = self._http.request_bytes(method, self._url(path), headers=self._auth_headers())
        self._raise_for_status(status_code, path=path, payload=content)
        return content

    @staticmethod
    def _raise_for_status(status_code: int, *, path: str, payload: Any = None) -> None:
        if status_code < 400:
            return
        # DocuSign returns the real reason (errorCode + message) in the JSON body of
        # a 4xx/5xx — e.g. ENVELOPE_HAS_INVALID_RECIPIENTS, ANCHOR_TAB_STRING_NOT_FOUND,
        # the dreaded "recipient has no tabs". The generic "HTTP 400" alone makes the
        # failure undiagnosable on prod, so extract that detail, fold it into the
        # raised message, and log a single sanitized (secret-free, capped) line.
        # Mirrors google_identity._json_request for Google OAuth 4xx bodies.
        detail = _docusign_error_detail(payload)
        if status_code == 404:
            raise DocuSignEnvelopeNotFoundError(_with_detail("DocuSign envelope not found.", detail))
        if status_code == 401:
            message = _with_detail("DocuSign authorization was rejected; reconnect DocuSign.", detail)
            _log_docusign_failure("DocuSign authorization rejected", status=status_code, path=path, detail=detail)
            raise DocuSignNotConnectedError(message)
        message = _with_detail(f"DocuSign API request failed (HTTP {status_code}).", detail)
        _log_docusign_failure("DocuSign API request failed", status=status_code, path=path, detail=detail)
        raise DocuSignError(message)


class _UrllibTransport:
    """Default HTTP transport over the stdlib (no third-party dependency)."""

    def request_json(
        self, method: str, url: str, *, headers: dict[str, str], json_body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                status_code = int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code), _safe_json(error.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DocuSignError("DocuSign API request failed.") from exc
        return status_code, _safe_json(raw)

    def request_bytes(self, method: str, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as error:
            # Keep the error body so a 4xx download surfaces DocuSign's real reason
            # (it is JSON even on the bytes endpoint) instead of a bare status code.
            return int(error.code), error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DocuSignError("DocuSign document download failed.") from exc


def _safe_json(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads((raw or b"").decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _docusign_error_detail(payload: Any) -> str:
    """Pull DocuSign's ``errorCode``/``message`` out of a 4xx/5xx response body.

    ``payload`` is whatever the transport handed back for the failed call: the
    JSON-decoded dict from :meth:`request_json`, or the raw bytes from
    :meth:`request_bytes` (the document endpoints). DocuSign's error body is
    ``{"errorCode": "...", "message": "..."}``; some validation errors nest the
    real cause under ``errorDetails``. Returns a short, single-line, secret-free
    description for both the raised message and the log, or "" if unreadable.
    """
    body: Any = payload
    if isinstance(payload, (bytes, bytearray)):
        body = _safe_json(bytes(payload))
    if not isinstance(body, dict):
        return ""
    error_code = str(body.get("errorCode") or "").strip()
    message = str(body.get("message") or "").strip()
    # Some 400s carry the specific offender (e.g. the recipient/tab) only in the
    # first nested errorDetails entry; fold its message in when the top-level one
    # is generic or missing.
    details = body.get("errorDetails")
    if isinstance(details, list) and details and isinstance(details[0], dict):
        nested = str(details[0].get("message") or "").strip()
        if nested and nested != message:
            message = f"{message}: {nested}" if message else nested
    detail = error_code
    if message and message != error_code:
        detail = f"{error_code}: {message}" if error_code else message
    return _sanitize_detail(detail)


def _sanitize_detail(detail: str) -> str:
    """Collapse to a single capped line so nothing multi-line/huge leaks to logs."""
    collapsed = " ".join(str(detail or "").split())
    return collapsed[:300]


def _with_detail(message: str, detail: str) -> str:
    return f"{message} ({detail})" if detail else message


def _log_docusign_failure(label: str, *, status: int, path: str, detail: str) -> None:
    """One sanitized stderr line per failure.

    Carries the status, the API path (which holds no token/secret — ids are already
    percent-escaped path segments), and DocuSign's errorCode/message. The bearer
    token lives only in a request header and is never part of any of these, so the
    line is safe to emit.
    """
    detail_part = f" detail={detail}" if detail else ""
    print(f"{label} status={status} path={path}{detail_part}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Factory — the running app always gets the real client
# ---------------------------------------------------------------------------
def get_client(*, owner_user_id: str) -> DocuSignClient:
    """Return the real eSignature client bound to ``owner_user_id``'s account.

    The running app ALWAYS uses the live DocuSign API. Raises
    :class:`DocuSignNotConnectedError` when the user has not connected DocuSign
    (no stored token / unresolved account), so the route can prompt connect.
    There is no demo or simulated client in this path.
    """
    account = docusign_connection.account_for_user(owner_user_id)
    return HttpDocuSignClient(
        owner_user_id=owner_user_id,
        account_id=account["account_id"],
        base_uri=account["base_uri"],
    )


def connection_status(*, owner_user_id: str) -> dict[str, Any]:
    """Connection state for ``GET /api/docusign/status``.

    ``connected`` reflects a real stored DocuSign token for the user;
    ``configured`` reflects whether the OAuth app credentials are set in env (so
    the connect button can even start). ``account_label`` is the resolved account
    name/email for the panel.

    Two diagnosability signals are also surfaced (both additive):

    * ``config_health`` — an OFFLINE check of whether the app credentials are
      present + well-formed (a typo'd integration key shows as ``client_id_malformed``
      instead of looking like a DocuSign outage at connect time).
    * ``needs_reconnect`` — set when the user's stored grant was found to be dead on
      the last token refresh (consent revoked / refresh expired). The panel can then
      prompt a reconnect instead of showing a generic outage.
    """
    configured = docusign_connection.oauth_configured()
    connected = bool(owner_user_id) and docusign_connection.is_connected(owner_user_id)
    account = docusign_connection.stored_account(owner_user_id) if connected else {}
    label = ""
    if connected:
        label = account.get("account_name") or account.get("email") or account.get("account_id") or "DocuSign"
    reconnect_required = connected and docusign_connection.needs_reconnect(owner_user_id)
    status = {
        "connected": connected,
        "configured": configured,
        "production": docusign_connection.is_production(),
        "auth_server": docusign_connection.auth_server(),
        "account_label": label,
        "account": account or {"account_id": "", "base_uri": "", "account_name": "", "email": ""},
        "config_health": docusign_connection.config_health(),
        "needs_reconnect": reconnect_required,
    }
    if reconnect_required:
        status["reconnect_message"] = (
            "Your DocuSign authorization is no longer valid (access was revoked or "
            "expired). Reconnect DocuSign to continue sending for signature."
        )
    return status


def _segment(value: str) -> str:
    """URL-path-escape an id segment so it can never inject extra path/query."""
    return urllib.parse.quote(str(value or "").strip(), safe="")


def _file_extension(filename: str) -> str:
    name = str(filename or "")
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()
    return "pdf"
