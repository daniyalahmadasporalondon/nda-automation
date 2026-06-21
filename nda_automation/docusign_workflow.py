"""Send-for-signature workflow — wire a matter's finalized NDA to DocuSign.

Two domain operations sit above :mod:`docusign_integration`:

* :func:`send_for_signature` — pull the matter's finalized NDA (reviewed/approved,
  else generated, else original), convert DOCX -> PDF when a converter is
  available (DocuSign signs a PDF best), derive the signers (counterparty contact
  from the matter + the Aspora signatory from the entity registry, both
  overridable), create + send the envelope, persist the envelope id/status on the
  matter, and transition the lifecycle to "sent, awaiting signature".
* :func:`sync_signature_status` — fetch the live envelope status; on ``completed``
  download the executed combined PDF, register it as the matter's ``signed``
  artifact (reusing :mod:`lifecycle_signed`), and transition the matter to
  executed/fully-signed. Best-effort + idempotent: a transient provider error
  never corrupts the matter.

The real :class:`docusign_integration.HttpDocuSignClient` is the default client
(via the factory). Tests inject the test double explicitly through ``client=``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Callable

from . import (
    artifact_registry,
    artifact_service,
    docusign_connection,
    docusign_integration,
    entity_registry,
    gmail_integration,
    gmail_matter_outbox,
    lifecycle_signed,
    nda_generation,
    workflow,
)
from .artifact_registry import (
    ROLE_GENERATED,
    ROLE_ORIGINAL,
    ROLE_REVIEWED,
    ROLE_SENT,
    latest_artifact_for_role,
    matter_artifacts,
)
from .docusign_integration import (
    DEFAULT_SIGNING_ORDER,
    STATUS_COMPLETED,
    STATUS_DECLINED,
    STATUS_VOIDED,
    DocuSignError,
    DocuSignNotConnectedError,
)
from .matter_repository import DiskMatterRepository, MatterRepository

logger = logging.getLogger(__name__)

# Where the signature envelope state lives on the matter (durable, owner-scoped).
SIGNATURE_FIELD = "docusign"

# Precedence for "the document we send to sign": the most-finalized version that
# exists. reviewed (post-approval) -> generated (a drafted NDA) -> sent (already
# emailed) -> original (the inbound copy).
_DOCUMENT_PRECEDENCE = (ROLE_REVIEWED, ROLE_GENERATED, ROLE_SENT, ROLE_ORIGINAL)

# The Aspora signatory placeholder needs a real email to receive the envelope.
# Falls back to this when the entity bundle has no concrete signatory email and
# the caller did not override; surfaced as a clear error rather than guessed.
_ASPORA_SIGNER_ROLE = "aspora"
_COUNTERPARTY_SIGNER_ROLE = "counterparty"

# DocuSign anchor strings the GENERATED NDA plants on each party's signature line
# (the single source is ``nda_generation`` so the written marker and the anchored
# tab can never drift). Each signer's signHere/dateSigned tab attaches to its
# party's token, so the field lands on the right line. These exist ONLY in a
# generated NDA; for received counterparty paper (a later phase) we have no
# control over the layout, so the anchors are applied ONLY when the document we
# send is a generated NDA (see ``_resolve_signers``) — otherwise the tabs fall
# back to the signer name as before.
_ANCHOR_FOR_ROLE = {
    _ASPORA_SIGNER_ROLE: nda_generation.SIGNATURE_ANCHOR_ASPORA,
    _COUNTERPARTY_SIGNER_ROLE: nda_generation.SIGNATURE_ANCHOR_COUNTERPARTY,
}

ClientFactory = Callable[..., docusign_integration.DocuSignClient]

# Best-effort Drive archive hook fired on the "completed" transition. Takes the
# executed matter + ids; returns nothing and must never raise (the live default
# swallows everything). Injectable so tests can assert it ran / simulate Drive
# being down without monkeypatching internals.
DriveSyncFn = Callable[..., None]


class DocuSignWorkflowError(RuntimeError):
    """A send-for-signature workflow step could not be completed."""


class MatterNotFoundError(DocuSignWorkflowError):
    pass


class NoSignableDocumentError(DocuSignWorkflowError):
    pass


class SignerResolutionError(DocuSignWorkflowError):
    pass


# The counterparty signer email can originate from an attacker-controlled inbound
# header (Reply-To/From) or untrusted intake free-text, so a send-for-signature
# that would email the finalized NDA to such an address must be confirmed against
# the resolved recipient first — exactly like the Gmail send-redline path. Reuse
# the SAME exception type the Gmail outbox raises so a single error contract
# governs both outbound channels (the route maps it to a 400).
RecipientConfirmationError = gmail_integration.RecipientConfirmationError


@dataclass(frozen=True)
class SendForSignatureResult:
    matter: dict[str, Any]
    envelope_id: str
    status: str
    signers: list[dict[str, Any]]
    document_filename: str


@dataclass(frozen=True)
class SignatureStatusResult:
    matter: dict[str, Any]
    envelope_id: str
    status: str
    completed: bool
    signed_artifact_id: str = ""


def send_for_signature(
    matter: dict[str, Any] | None,
    matter_id: str,
    owner_user_id: str = "",
    *,
    signers: Any | None = None,
    signing_order: str = DEFAULT_SIGNING_ORDER,
    email_subject: str = "",
    confirm_recipient: str | None = None,
    repository: MatterRepository | None = None,
    client: docusign_integration.DocuSignClient | None = None,
    client_factory: ClientFactory | None = None,
) -> SendForSignatureResult:
    """Create + send a DocuSign envelope for a matter's finalized NDA.

    Resolves the document (most-finalized artifact, DOCX converted to PDF when a
    converter is available), the signers (override > counterparty contact + Aspora
    signatory), creates the envelope via the (real, by default) client, persists
    the envelope id/status under ``matter["docusign"]``, and flips the matter to
    "sent, awaiting signature".

    ``client`` lets a test inject the test double; otherwise the real eSignature
    client is built for ``owner_user_id`` via the factory.
    """
    repository = repository or DiskMatterRepository()
    matter = _load_matter(matter, matter_id, owner_user_id, repository)

    document_bytes, document_filename = _resolve_signable_document(
        matter, matter_id, owner_user_id, repository
    )
    # The single, mode-aware normalization point: rank the raw resolved signers
    # ONCE with the REAL signing order. ``_resolve_signers`` deliberately returns
    # un-normalized entries so this is the only place the routing order is assigned
    # (create_envelope re-validates idempotently). This is what makes "sequential"
    # actually route 1,2 instead of silently collapsing to parallel.
    effective_order = (
        signing_order if signing_order in docusign_integration.SIGNING_ORDERS else DEFAULT_SIGNING_ORDER
    )
    resolved_signers = docusign_integration.normalize_signers(
        _resolve_signers(matter, signers), signing_order=effective_order
    )
    # SECURITY GATE (mirrors the Gmail send-redline path): the counterparty signer
    # email can be derived from an attacker-controlled inbound header
    # (Reply-To/From) or untrusted intake free-text. DocuSign dispatches the
    # finalized NDA to every signer the moment the envelope is created (status
    # "sent"), so a send that would email a SPOOFABLE-derived address must be
    # confirmed by the operator against the resolved recipient BEFORE any DocuSign
    # API call. When the operator typed a genuinely different address (an explicit
    # override that does not match the spoofable inbound value) nothing untrusted is
    # being trusted, so it proceeds unchanged. Raises RecipientConfirmationError ->
    # the route returns 400 with NO envelope created and NO state change.
    _require_confirmed_counterparty(matter, resolved_signers, confirm_recipient)
    subject = str(email_subject or "").strip() or _default_subject(matter)

    docusign_client = _client(client, client_factory, owner_user_id)
    try:
        created = docusign_client.create_envelope(
            document_bytes,
            document_filename,
            resolved_signers,
            signing_order=effective_order,
            email_subject=subject,
        )
    except DocuSignNotConnectedError:
        raise
    except DocuSignError as error:
        raise DocuSignWorkflowError(str(error)) from error

    envelope_id = str(created.get("envelope_id") or "")
    status = str(created.get("status") or "")
    if not envelope_id:
        raise DocuSignWorkflowError("DocuSign did not return an envelope id.")

    signature_block = {
        "envelope_id": envelope_id,
        "status": status,
        # Persist the EFFECTIVE order actually sent (an unknown/blank request value
        # falls back to the default) so the stored record matches the envelope.
        "signing_order": effective_order,
        "email_subject": subject,
        "document_filename": document_filename,
        "signers": [signer.to_dict() for signer in resolved_signers],
        "sent_at": _now_iso(),
        "last_synced_at": _now_iso(),
        "provider": "docusign",
    }
    updated = repository.update_matter_fields(
        matter_id,
        {
            SIGNATURE_FIELD: signature_block,
            # Drive the workflow "sent, awaiting signature" state: a recorded
            # outbound + the sent board column read as sent_awaiting_counterparty
            # in workflow.py without inventing a new status.
            "board_column": "sent",
            "awaiting_signature": True,
            # Re-send after a terminal envelope: the matter may carry stale
            # void/decline flags written by sync_signature_status. A FRESH
            # envelope is now in flight, so clear those terminal flags (and their
            # stamps) the same way the terminal transitions clear each other.
            # Without this, _derive_phase_and_status's signature_voided /
            # signature_declined checks (which run before _sent_status) out-rank
            # the fresh outbound and mislabel the matter as Voided/Declined.
            "signature_voided": False,
            "signature_voided_at": None,
            "signature_declined": False,
            "signature_declined_at": None,
        },
        owner_user_id=owner_user_id,
    )
    if updated is None:
        # The envelope is already out; persisting state failed. Surface the live
        # state rather than pretend it did not send.
        updated = {**matter, SIGNATURE_FIELD: signature_block}

    return SendForSignatureResult(
        matter=updated,
        envelope_id=envelope_id,
        status=status,
        signers=[signer.to_dict() for signer in resolved_signers],
        document_filename=document_filename,
    )


def sync_signature_status(
    matter: dict[str, Any] | None,
    matter_id: str,
    owner_user_id: str = "",
    *,
    repository: MatterRepository | None = None,
    client: docusign_integration.DocuSignClient | None = None,
    client_factory: ClientFactory | None = None,
    drive_sync: DriveSyncFn | None = None,
    drive_token_owner_user_id: str | None = None,
) -> SignatureStatusResult:
    """Fetch the envelope status; on completion store the signed artifact.

    Reads the persisted envelope id, fetches the live status, and updates the
    stored status. When DocuSign reports ``completed`` it downloads the executed
    combined PDF, registers it as the matter's ``signed`` artifact (via
    :mod:`lifecycle_signed`, eager + best-effort), and flips the matter to executed
    by routing the triad write through the SHARED
    :func:`lifecycle_signed.mark_matter_executed` primitive -- the same primitive
    the manual-mark and signed-upload paths use. That convergence is what makes the
    DocuSign-executed state IDENTICAL to those paths: the executed triad
    (``executed`` / ``executed_at`` / ``status=fully_signed``), a CLEARED stale
    ``workflow_error``, and exactly ONE ``executed`` timeline event (actor
    ``DocuSign``).

    Idempotent: re-syncing an ALREADY-executed matter is a status refresh only --
    it does NOT re-download / re-capture the signed PDF (no artifact v1->v2 churn),
    does NOT re-flip the triad or append a second ``executed`` event, and does NOT
    re-archive to Drive. The returned ``signed_artifact_id`` re-points to the
    artifact captured on first completion.

    On the same ``completed`` transition it ALSO archives the executed matter to
    Google Drive (the signed PDF + a fresh ``metadata/matter_summary.json``) via
    :func:`_archive_to_drive`. That archive is strictly best-effort: a Drive
    outage / not-connected / any error is swallowed and logged, never blocking the
    executed transition (the envelope IS done at DocuSign). ``drive_sync`` is
    injectable for tests; it defaults to the live Drive archiver.

    ``drive_token_owner_user_id`` is the GOOGLE-token owner the Drive upload
    authenticates as (distinct from the matter owner). The status-poll route passes
    the session's connected-Google id (``_google_owner_user_id``) so the archive
    resolves the Drive token the SAME way the deliberate Save-to-Drive route does,
    instead of mis-using the matter/request id (the #10 fix). When ``None`` the
    archiver resolves it from the matter owner's stored Drive token, falling back to
    the server-global token (the no-login / webhook path).
    """
    repository = repository or DiskMatterRepository()
    matter = _load_matter(matter, matter_id, owner_user_id, repository)

    signature = matter.get(SIGNATURE_FIELD)
    if not isinstance(signature, dict) or not signature.get("envelope_id"):
        raise DocuSignWorkflowError("NDA has no DocuSign envelope to sync.")
    envelope_id = str(signature.get("envelope_id") or "")

    docusign_client = _client(client, client_factory, owner_user_id)
    try:
        status = str(docusign_client.get_envelope_status(envelope_id) or "")
    except DocuSignNotConnectedError:
        raise
    except DocuSignError as error:
        raise DocuSignWorkflowError(str(error)) from error

    completed = status == STATUS_COMPLETED
    # Idempotency short-circuit: a re-sync of an envelope that ALREADY flipped the
    # matter to executed must not re-churn anything. Skipping below means we don't
    # re-download + re-capture the signed PDF (which bumps the artifact v1->v2),
    # don't re-flip the triad / re-append a second ``executed`` event, and don't
    # re-archive to Drive on every poll. We still re-confirm the live envelope
    # status / last_synced / per-recipient signers below (a cheap status refresh).
    already_executed = completed and workflow._is_executed(matter)
    signed_artifact_id = ""
    fields: dict[str, Any] = {SIGNATURE_FIELD: {**signature, "status": status, "last_synced_at": _now_iso()}}

    # Surface the PER-RECIPIENT signed state so the matter view can show each
    # party (Aspora / counterparty) at a glance — signed / awaiting — rather than
    # only the envelope's overall status. Best-effort: a client that does not
    # expose recipients, or any fetch failure, leaves the stored signers untouched
    # (the per-party section degrades to the existing overall status). Never blocks
    # the sync — the envelope status above is the authoritative transition.
    fields[SIGNATURE_FIELD]["signers"] = _signers_with_recipient_status(
        signature, docusign_client, envelope_id
    )

    # Terminal-but-not-signed transitions, split by what they mean for the deal.
    # Both clear the awaiting-signature limbo (so a dead/cancelled deal stops
    # reading as "awaiting counterparty" forever) and record a timeline event; they
    # diverge on the resulting workflow state (see workflow._derive_phase_and_status).
    declined = status == STATUS_DECLINED
    voided = status == STATUS_VOIDED
    timeline_event: dict[str, Any] | None = None

    if completed:
        # Clear the awaiting-counterparty limbo on every completed sync. The
        # executed TRIAD (executed / executed_at / status=fully_signed), the
        # workflow_error clear, and the single ``executed`` timeline event are NOT
        # written here -- they are routed through the shared
        # ``lifecycle_signed.mark_matter_executed`` primitive below so the DocuSign
        # path produces an IDENTICAL executed state to the manual-mark / signed-
        # upload paths (same triad + same cleared error + exactly one executed
        # event). We only capture the signed PDF on the FIRST completion; a re-sync
        # of an already-executed matter skips the re-download/re-capture churn.
        fields["awaiting_signature"] = False
        if not already_executed:
            signed_artifact_id = _capture_executed_document(
                docusign_client,
                envelope_id,
                matter,
                matter_id,
                owner_user_id,
                repository,
                signature,
            )
            if signed_artifact_id:
                fields[SIGNATURE_FIELD]["signed_artifact_id"] = signed_artifact_id
        else:
            # Already-executed re-sync: report the artifact captured on first
            # completion (we did NOT re-capture) so the result stays meaningful.
            signed_artifact_id = str(signature.get("signed_artifact_id") or "")
    elif declined:
        # Counterparty REFUSED. Clear awaiting; flag it for human attention. The
        # matter stays visible (Sent column) and needs_attention so the user can
        # renegotiate, re-send, or close. NOT executed.
        fields["awaiting_signature"] = False
        fields["signature_declined"] = True
        fields["signature_declined_at"] = _now_iso()
        # A re-decline / re-void should never leave the opposite stale flag set.
        fields["signature_voided"] = False
        timeline_event = _signature_terminal_event(
            declined=True, voided=False, raw_status=status
        )
    elif voided:
        # Envelope CANCELLED (usually the sender voided to reissue). Clear awaiting
        # and return the matter to a RE-SENDABLE state (Send available again). NOT
        # an error / attention state.
        fields["awaiting_signature"] = False
        fields["signature_voided"] = True
        fields["signature_voided_at"] = _now_iso()
        fields["signature_declined"] = False
        timeline_event = _signature_terminal_event(
            declined=False, voided=True, raw_status=status
        )

    updated = repository.update_matter_fields(matter_id, fields, owner_user_id=owner_user_id)
    if updated is None:
        updated = {**matter, **fields}

    if timeline_event is not None:
        # Append the terminal-not-signed event after the state write so the matter
        # exists with the cleared flags. Best-effort: never let a timeline hiccup
        # mask the (already-persisted) status transition.
        try:
            after_event = repository.append_timeline_event(
                matter_id, timeline_event, owner_user_id=owner_user_id
            )
        except Exception:  # noqa: BLE001 -- the timeline is non-authoritative here.
            after_event = None
        if after_event is not None:
            updated = after_event

    if completed and not already_executed:
        # Converge the executed flip onto the ONE shared primitive: it sets the
        # triad (executed / executed_at / status=fully_signed), CLEARS any stale
        # ``workflow_error`` (the board-vs-detail disagreement the manual path
        # already fixes), and appends EXACTLY ONE ``executed`` timeline event with
        # an actor + DocuSign-specific detail. Its own ``_is_executed`` guard makes
        # it a no-op if a race already executed the matter, so it can never double-
        # write the triad or double-append the event. The signature-specific fields
        # (status / signers / signed_artifact_id / awaiting_signature) were already
        # persisted in the field write above.
        executed = lifecycle_signed.mark_matter_executed(
            repository,
            matter_id,
            owner_user_id,
            actor="DocuSign",
            detail="Executed via DocuSign (all parties signed).",
        )
        if executed is not None:
            updated = executed

        # Best-effort: archive the fully-executed matter (signed PDF + refreshed
        # matter_summary.json) to its Drive folder. Runs AFTER the matter is
        # persisted so the signed artifact + executed fields are in the registry
        # that sync_matter_folder reads. A Drive outage / not-connected / any
        # error is swallowed inside the archiver and NEVER touches the executed
        # transition above. Gated on the FIRST completion (not already_executed) so
        # a re-sync of an executed matter does not re-archive on every poll.
        archiver = drive_sync or _archive_to_drive
        archiver(
            matter=updated,
            matter_id=matter_id,
            owner_user_id=owner_user_id,
            repository=repository,
            drive_token_owner_user_id=drive_token_owner_user_id,
            signed_via="docusign",
        )

    return SignatureStatusResult(
        matter=updated,
        envelope_id=envelope_id,
        status=status,
        completed=completed,
        signed_artifact_id=signed_artifact_id,
    )


# ---------------------------------------------------------------------------
# Document resolution
# ---------------------------------------------------------------------------
def _resolve_signable_document(
    matter: dict[str, Any],
    matter_id: str,
    owner_user_id: str,
    repository: MatterRepository,
) -> tuple[bytes, str]:
    """The most-finalized NDA bytes + a filename, PDF-preferred for signing.

    Walks the artifact precedence (reviewed -> generated -> sent -> original) for
    the first artifact with retrievable bytes. DOCX is converted to PDF when a
    converter is available (DocuSign tabs anchor reliably on a PDF); if conversion
    is unavailable we send the bytes we have so the flow never hard-blocks.
    """
    artifact, file_bytes = _latest_artifact_with_bytes(matter, matter_id, owner_user_id, repository)
    if artifact is None or not file_bytes:
        # Fall back to the matter's raw source document if the registry is empty.
        source_bytes = repository.get_source_document_bytes(matter)
        source_filename = str(matter.get("source_filename") or matter.get("stored_filename") or "NDA.docx")
        if not source_bytes:
            raise NoSignableDocumentError("This NDA has no finalized document to send for signature.")
        return _as_pdf(source_bytes, source_filename, owner_user_id)

    ext = (artifact.ext or "").lower()
    base_filename = str(matter.get("source_filename") or matter.get("document_title") or "NDA")
    stem = Path(base_filename).stem or "NDA"
    filename = f"{stem}.{ext or 'docx'}"
    return _as_pdf(file_bytes, filename, owner_user_id)


def _latest_artifact_with_bytes(
    matter: dict[str, Any],
    matter_id: str,
    owner_user_id: str,
    repository: MatterRepository,
):
    for role in _DOCUMENT_PRECEDENCE:
        artifact = latest_artifact_for_role(matter, role)
        if artifact is None:
            continue
        file_bytes = artifact_service.get_artifact_bytes(
            matter_id, artifact.id, repository=repository, owner_user_id=owner_user_id
        )
        if file_bytes:
            return artifact, file_bytes
    return None, b""


def _as_pdf(file_bytes: bytes, filename: str, owner_user_id: str) -> tuple[bytes, str]:
    """Return PDF bytes + a .pdf filename, converting from DOCX when possible.

    Already-PDF bytes pass through. DOCX is converted via the existing PDF export
    helper when LibreOffice is available; if conversion is unavailable we return
    the original bytes (the real DocuSign API still accepts a DOCX document — it
    just renders less reliably for anchor tabs).
    """
    ext = Path(filename).suffix.lower()
    if ext == ".pdf" or file_bytes[:5] == b"%PDF-":
        return file_bytes, _with_ext(filename, "pdf")
    try:
        from . import pdf_export_service

        export = pdf_export_service.build_docx_pdf_export(file_bytes, filename, owner_user_id=owner_user_id)
        pdf_bytes = Path(export.path).read_bytes()
        if pdf_bytes:
            return pdf_bytes, _with_ext(filename, "pdf")
    except Exception:
        # Converter unavailable / conversion failed — send the source bytes so the
        # signature flow degrades gracefully instead of hard-blocking.
        pass
    return file_bytes, filename


def _with_ext(filename: str, ext: str) -> str:
    stem = Path(str(filename or "NDA")).stem or "NDA"
    return f"{stem}.{ext}"


# ---------------------------------------------------------------------------
# Per-recipient signature status
# ---------------------------------------------------------------------------
def _signers_with_recipient_status(
    signature: dict[str, Any],
    client: docusign_integration.DocuSignClient,
    envelope_id: str,
) -> list[dict[str, Any]]:
    """The stored signer list enriched with each recipient's live signed status.

    Returns a fresh signer-dict list (copies of the stored ``signature["signers"]``)
    with two fields merged in per signer from DocuSign's recipients endpoint,
    matched by email (case-insensitively):

    * ``signature_status`` — ``signed`` / ``awaiting`` / ``declined`` (normalized).
    * ``signed_at`` — the recipient's signedDateTime when present, else ``""``.

    The signer's ``role`` (``aspora`` / ``counterparty``) is preserved verbatim, so
    the UI can map each party's signed state without re-deriving who is who.

    Best-effort and fail-soft: a client without ``get_envelope_recipients``, or any
    DocuSign error, returns the stored signers unchanged (so the view degrades to
    the envelope's overall status). Never raises.
    """
    stored = signature.get("signers")
    signers = [dict(s) for s in stored if isinstance(s, dict)] if isinstance(stored, list) else []
    fetch = getattr(client, "get_envelope_recipients", None)
    if not callable(fetch):
        return signers
    try:
        recipients = fetch(envelope_id)
    except DocuSignNotConnectedError:
        raise
    except DocuSignError:
        return signers
    except Exception:  # noqa: BLE001 -- per-recipient status is non-authoritative; never block the sync.
        return signers
    if not isinstance(recipients, list):
        return signers
    by_email = {
        str(r.get("email") or "").strip().casefold(): r
        for r in recipients
        if isinstance(r, dict) and str(r.get("email") or "").strip()
    }
    for signer in signers:
        match = by_email.get(str(signer.get("email") or "").strip().casefold())
        if match is None:
            continue
        signer["signature_status"] = str(match.get("status") or docusign_integration.RECIPIENT_AWAITING)
        signer["signed_at"] = str(match.get("signed_at") or "")
    return signers


# ---------------------------------------------------------------------------
# Signer resolution
# ---------------------------------------------------------------------------
def _resolve_signers(matter: dict[str, Any], override: Any | None) -> list[Any]:
    """Derive the envelope's recipients as RAW (un-normalized) signer entries.

    Returns the signer LIST without assigning routing orders — normalization (and
    thus the routing-order ranking) happens EXACTLY ONCE downstream in
    ``create_envelope`` with the REAL ``signing_order`` threaded from the request.
    Normalizing here too (the old behaviour) defaulted to parallel and stamped
    ``routing_order=1`` on every signer, which then SILENTLY COLLAPSED a later
    "sequential" send back to parallel (the second normalize saw an order already
    set and left it). Keeping these raw lets the single downstream normalize rank
    them from the chosen mode.

    When ``override`` is supplied (a non-empty list of {name,email[,anchor,role]})
    it is used verbatim (after role-stamping). Otherwise we build the two-party
    signer set: the counterparty contact (from the matter's reply/sender + derived
    counterparty name) and the Aspora signatory (from the entity registry bundle,
    when one is selected on the matter).

    Signature-field anchoring: a GENERATED NDA carries a distinct, per-party
    anchor token on each signature line (planted by ``nda_generation``), so each
    signer's signHere/dateSigned tab attaches to the correct line. We assign those
    anchors here, but ONLY when the document being sent is a generated NDA (it is
    the case we control the template for). For received counterparty paper the
    tokens are not in the document, so we leave the anchor empty and the tabs fall
    back to the signer name — the reliable-anchor work for third-party layouts is
    a later phase.
    """
    if isinstance(override, list) and override:
        return _stamp_override_roles(override)

    generated = _is_generated_nda_matter(matter)

    signers: list[dict[str, Any]] = []

    counterparty = _counterparty_signer(matter)
    if counterparty is not None:
        signers.append(counterparty)

    aspora = _aspora_signer(matter)
    if aspora is not None:
        signers.append(aspora)

    if not signers:
        raise SignerResolutionError(
            "Could not resolve any signers; provide signers explicitly "
            "({name, email}) to send this NDA for signature."
        )

    if generated:
        for signer in signers:
            anchor = _ANCHOR_FOR_ROLE.get(signer.get("role") or "")
            if anchor:
                signer["anchor"] = anchor

    return signers


# The Aspora internal-signer domain. Any signer at this domain is the Aspora party
# (never the counterparty), independent of whether a default-signer email is
# configured — the belt to the configured-email suspenders.
_ASPORA_SIGNER_DOMAIN = "aspora.com"


def _is_aspora_signer_email(email: str) -> bool:
    """True when ``email`` is the Aspora internal signer (config match or domain).

    Identifies the Aspora party by either the configured default Aspora signer
    address (``NDA_DOCUSIGN_ASPORA_SIGNER_EMAIL`` via
    :func:`docusign_connection.aspora_default_signer`) OR the ``aspora.com``
    domain. Pure + defensive: a blank/odd value is not Aspora.
    """
    normalized = str(email or "").strip().casefold()
    if not normalized or "@" not in normalized:
        return False
    default_signer = docusign_connection.aspora_default_signer()
    if default_signer is not None:
        configured = str(default_signer.get("email") or "").strip().casefold()
        if configured and normalized == configured:
            return True
    domain = normalized.rsplit("@", 1)[-1]
    return domain == _ASPORA_SIGNER_DOMAIN


def _stamp_override_roles(override: list[Any]) -> list[Any]:
    """Stamp signer roles on a client-supplied override so the Aspora party is labelled.

    The send route accepts ``signers`` with ``role`` optional, so an override can
    arrive with blank roles. If the Aspora internal signer is listed first with a
    blank role, the read-side counterparty helper (matter_view) could otherwise
    pick it as the counterparty. We fix this at the SOURCE: any signer whose email
    is the Aspora internal signer (configured email or ``aspora.com`` domain) is
    stamped ``role="aspora"``; every other signer with a blank role is stamped
    ``role="counterparty"``.

    Preserves already-correct overrides: an explicit non-blank role is left
    untouched (we never relabel a deliberately-set role). Only the recorded
    ``role`` label changes — name/email/anchor/routing and thus WHO receives the
    envelope are untouched. Non-dict / ``Signer`` entries pass through unchanged
    (``normalize_signers`` handles them); a ``Signer`` with a blank role gets the
    same stamp so persisted recipients carry the right label.
    """
    stamped: list[Any] = []
    for raw in override:
        if isinstance(raw, dict):
            email = raw.get("email")
            existing_role = str(raw.get("role") or "").strip()
            entry = dict(raw)
            if _is_aspora_signer_email(email):
                entry["role"] = _ASPORA_SIGNER_ROLE
            elif not existing_role:
                entry["role"] = _COUNTERPARTY_SIGNER_ROLE
            stamped.append(entry)
        elif isinstance(raw, docusign_integration.Signer):
            existing_role = str(raw.role or "").strip()
            if _is_aspora_signer_email(raw.email):
                raw.role = _ASPORA_SIGNER_ROLE
            elif not existing_role:
                raw.role = _COUNTERPARTY_SIGNER_ROLE
            stamped.append(raw)
        else:
            stamped.append(raw)
    return stamped


def _require_confirmed_counterparty(
    matter: dict[str, Any],
    resolved_signers: list[Any],
    confirm_recipient: str | None,
) -> None:
    """Block a send to a spoofable-derived counterparty unless it is confirmed.

    The DocuSign analogue of the Gmail outbox's ``require_confirmed_recipient``.
    We compute the SPOOFABLE inbound/intake-derived recipient
    (``matter_reply_recipient`` reads ``reply_to``/``sender``, both of which are
    written verbatim from the attacker-controlled inbound ``Reply-To``/``From``
    header and from untrusted intake free-text). If the envelope's resolved
    counterparty email EQUALS that spoofable value, the finalized NDA is about to be
    emailed to an untrusted address, so we demand a ``confirm_recipient`` that
    matches it (raising :class:`RecipientConfirmationError` on missing/mismatch
    BEFORE any DocuSign API call). When the resolved counterparty email is a
    genuinely different, operator-typed address (an explicit ``signers`` override
    that diverges from the spoofable value), nothing untrusted is being trusted and
    the send proceeds unchanged — the operator already vouched for that address by
    typing it.

    Reuses the Gmail outbox helper verbatim (same matching + error type) so the two
    outbound channels share one confirmation contract instead of forking the logic.
    """
    counterparty_email = _resolved_counterparty_email(resolved_signers)
    if not counterparty_email:
        # No routable counterparty recipient (e.g. an Aspora-only override): there is
        # no spoofable inbound address being emailed, so there is nothing to confirm.
        return
    spoofable_email = gmail_integration.matter_reply_recipient(matter)
    recipient_from_inbound_header = bool(
        spoofable_email
        and gmail_matter_outbox.email_addresses_match(counterparty_email, spoofable_email)
    )
    if not recipient_from_inbound_header:
        # The counterparty address the envelope will email is NOT the spoofable
        # inbound/intake value — it is an operator-typed override. Keep current
        # behaviour; the operator already chose this exact address.
        return
    gmail_matter_outbox.require_confirmed_recipient(
        counterparty_email,
        confirm_recipient,
        transport=gmail_integration,
        recipient_from_inbound_header=True,
    )


def _resolved_counterparty_email(resolved_signers: list[Any]) -> str:
    """The routable counterparty signer email from a normalized signer list.

    Prefers the signer explicitly labelled ``role == "counterparty"`` (set by
    ``_resolve_signers``/``_stamp_override_roles``); falls back to the first
    non-Aspora signer so an override with blank roles is still covered. Returns the
    canonicalized address ("" when none / unparseable)."""
    aspora_role = _ASPORA_SIGNER_ROLE
    fallback = ""
    for signer in resolved_signers:
        email = gmail_integration.recipient_email(getattr(signer, "email", ""))
        if not email:
            continue
        role = str(getattr(signer, "role", "") or "").strip().casefold()
        if role == _COUNTERPARTY_SIGNER_ROLE:
            return email
        if role != aspora_role and not fallback:
            fallback = email
    return fallback


def _counterparty_signer(matter: dict[str, Any]) -> dict[str, Any] | None:
    email = gmail_integration.matter_reply_recipient(matter)
    if not email:
        return None
    name = artifact_registry.derive_counterparty(matter) or email
    # The anchor is assigned in _resolve_signers (only for generated NDAs, where
    # the counterparty signature line carries the token).
    return {"name": name, "email": email, "role": _COUNTERPARTY_SIGNER_ROLE, "anchor": ""}


def _aspora_signer(matter: dict[str, Any]) -> dict[str, Any] | None:
    """The Aspora signatory for an Aspora-signing matter, when routable.

    Resolves the entity id from ``matter["signing_entity_id"]`` when set, else from
    the generated NDA's manifest (``...['generation']['entity_id']`` on the
    generated artifact — the matter-level intake_metadata drops unknown keys, so
    the artifact manifest is the reliable source for a generated matter). A
    resolvable entity id is what marks this as an Aspora-signing matter.

    Routing identity, in precedence order:

    1. A SINGLE default Aspora signatory from config
       (``NDA_DOCUSIGN_ASPORA_SIGNER_NAME`` + ``NDA_DOCUSIGN_ASPORA_SIGNER_EMAIL``):
       when BOTH are set it is used for ANY Aspora entity, standing in for the
       per-entity registry signatory (a ``[Authorised Signatory]`` placeholder with
       no email, which DocuSign cannot route to). This makes Aspora a routable
       signer on every generated NDA.
    2. Otherwise the selected entity's own registry signatory, but only when it is
       concrete + routable (a real name and ``@`` email).

    When neither yields a routable address the Aspora signer is OMITTED (parallel
    signing means the counterparty can still sign; an explicit per-send override
    still wins, handled earlier in :func:`_resolve_signers`) — fully backward
    compatible with the no-config behaviour.

    The anchor is assigned centrally in :func:`_resolve_signers` (only for a
    generated NDA, whose signature block carries the per-party token).
    """
    entity_id = _matter_entity_id(matter)
    if not entity_id:
        return None

    default_signer = docusign_connection.aspora_default_signer()
    if default_signer is not None:
        # One configured identity for every Aspora entity — overrides the per-entity
        # registry placeholder (which has no routable email).
        return {
            "name": default_signer["name"],
            "email": default_signer["email"],
            "role": _ASPORA_SIGNER_ROLE,
            "anchor": "",
        }

    entity = entity_registry.get_entity(entity_id)
    if not isinstance(entity, dict):
        return None
    signatory = entity.get("signatory") if isinstance(entity.get("signatory"), dict) else {}
    name = str(signatory.get("name") or "").strip()
    email = str(signatory.get("email") or "").strip()
    if not email or name.startswith("[") or "@" not in email:
        # No concrete, routable signatory — omit (parallel signing means the
        # counterparty can still sign; Aspora is added via override when known).
        return None
    return {"name": name, "email": email, "role": _ASPORA_SIGNER_ROLE, "anchor": ""}


def _matter_entity_id(matter: dict[str, Any]) -> str:
    """The Aspora signing-entity id for a matter, preferring an explicit field.

    ``matter["signing_entity_id"]`` is honoured first (an explicit selection);
    otherwise the generated NDA's manifest entity id is used, so a matter created
    by the generation flow (which records the entity on the artifact manifest, not
    a top-level field) still resolves its Aspora signer.
    """
    explicit = str(matter.get("signing_entity_id") or "").strip()
    if explicit:
        return explicit
    manifest = _generation_manifest(matter)
    return str(manifest.get("entity_id") or "").strip()


def _generation_manifest(matter: dict[str, Any]) -> dict[str, Any]:
    """The generation manifest from the matter's generated artifact, or ``{}``.

    Generated NDAs stash the manifest on the generated artifact's
    ``metadata['generation']``. Returns the first one found (a matter has at most
    one generated NDA), or an empty dict for a non-generated matter.
    """
    for artifact in matter_artifacts(matter):
        metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
        generation = metadata.get("generation")
        if isinstance(generation, dict) and generation:
            return generation
    return {}


def _is_generated_nda_matter(matter: dict[str, Any]) -> bool:
    """Whether the document we will send is a generated NDA (carries our anchors).

    True iff the matter has a generated NDA artifact with a manifest. The reviewed
    (post-approval) document is derived from the generated NDA and keeps the same
    signature block + anchor tokens, so a generation manifest is the right signal
    even when the reviewed copy wins the send-precedence walk.
    """
    return bool(_generation_manifest(matter))


# ---------------------------------------------------------------------------
# Executed-document capture
# ---------------------------------------------------------------------------
def _capture_executed_document(
    docusign_client: docusign_integration.DocuSignClient,
    envelope_id: str,
    matter: dict[str, Any],
    matter_id: str,
    owner_user_id: str,
    repository: MatterRepository,
    signature: dict[str, Any],
) -> str:
    """Download the executed PDF and register it as the matter's signed artifact.

    Best-effort: a download or capture hiccup must never block the
    "completed" transition (the envelope IS done at DocuSign). Reuses
    :func:`lifecycle_signed.capture_signed_artifact` so the executed copy lands as
    a terminal ``signed`` artifact exactly like a manual upload would.
    """
    try:
        pdf_bytes = docusign_client.download_completed(envelope_id)
    except DocuSignError:
        return ""
    if not pdf_bytes:
        return ""
    filename = f"{Path(str(signature.get('document_filename') or 'NDA')).stem or 'NDA'}-executed.pdf"
    try:
        from . import lifecycle_signed

        artifact = lifecycle_signed.capture_signed_artifact(
            repository, matter_id, owner_user_id, pdf_bytes, filename
        )
    except Exception:
        return ""
    return artifact.id if artifact is not None else ""


def _archive_to_drive(
    *,
    matter: dict[str, Any],
    matter_id: str,
    owner_user_id: str,
    repository: MatterRepository,
    drive_token_owner_user_id: str | None = None,
    signed_via: str = "docusign",
) -> None:
    """Mirror the fully-executed matter into its Google Drive folder (best-effort).

    Fires on the DocuSign ``completed`` transition so the executed PDF + a fresh
    ``metadata/matter_summary.json`` land in ``{root}/{counterparty}/{matter}/``.
    A thin wrapper over the shared :func:`drive_integration.archive_executed_matter`
    (the single archiver every executed transition shares).

    ``owner_user_id`` is the MATTER owner (artifact bytes + write-back);
    ``drive_token_owner_user_id`` is the GOOGLE-token owner the upload authenticates
    as. The status-poll route resolves the latter from the session
    (``_google_owner_user_id``); the webhook resolves it from the matched matter's
    connected Google account. When not supplied the archiver resolves it from the
    matter owner's stored Drive token, falling back to the server-global token —
    which keeps the no-login / local-demo ``""`` path working.

    STRICTLY best-effort: the shared archiver swallows + LOGS every failure path
    (not connected, auto-intake off, settings read blowing up, the sync raising,
    the write-back failing) and never raises, so a Drive outage can never block or
    fail the "completed"/executed transition that already persisted before us.
    """
    from . import drive_integration

    drive_integration.archive_executed_matter(
        matter=matter,
        matter_id=matter_id,
        owner_user_id=owner_user_id,
        repository=repository,
        drive_token_owner_user_id=drive_token_owner_user_id,
        signed_via=signed_via,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _client(
    client: docusign_integration.DocuSignClient | None,
    client_factory: ClientFactory | None,
    owner_user_id: str,
) -> docusign_integration.DocuSignClient:
    if client is not None:
        return client
    factory = client_factory or docusign_integration.get_client
    return factory(owner_user_id=owner_user_id)


def _load_matter(
    matter: dict[str, Any] | None,
    matter_id: str,
    owner_user_id: str,
    repository: MatterRepository,
) -> dict[str, Any]:
    if isinstance(matter, dict):
        return matter
    loaded = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if loaded is None:
        raise MatterNotFoundError("NDA not found.")
    return loaded


def _default_subject(matter: dict[str, Any]) -> str:
    counterparty = artifact_registry.derive_counterparty(matter)
    title = str(matter.get("document_title") or matter.get("subject") or "NDA").strip() or "NDA"
    if counterparty and counterparty.lower() != "unknown counterparty":
        return f"Please sign: {title} — {counterparty}"
    return f"Please sign: {title}"


def _signature_terminal_event(
    *, declined: bool, voided: bool, raw_status: str
) -> dict[str, Any]:
    """Build the timeline event for a terminal-but-not-signed signature transition.

    Declined and voided each get a distinct, human-readable detail line and land on
    the workflow phase/status the deriver will read for them (Sent/declined for a
    refusal, Approval/voided for a cancelled-re-sendable envelope). Uses the shared
    ``workflow.build_timeline_event`` so the shape matches every other lifecycle
    event in the append-only log.
    """
    from . import workflow

    now = _now_iso()
    date = now[:10]
    if declined:
        return workflow.build_timeline_event(
            "signature_declined",
            phase=workflow.PHASE_SENT,
            status=workflow.STATUS_SIGNATURE_DECLINED,
            actor="docusign",
            detail=f"Counterparty declined on {date}.",
            at=now,
        )
    # voided
    return workflow.build_timeline_event(
        "signature_voided",
        phase=workflow.PHASE_APPROVAL,
        status=workflow.STATUS_SIGNATURE_VOIDED,
        actor="docusign",
        detail=f"Envelope voided on {date}.",
        at=now,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
