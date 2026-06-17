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
from pathlib import Path
from typing import Any, Callable

from . import (
    artifact_registry,
    artifact_service,
    docusign_connection,
    docusign_integration,
    entity_registry,
    gmail_integration,
    nda_generation,
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
    Signer,
    DocuSignError,
    DocuSignNotConnectedError,
)
from .matter_repository import DiskMatterRepository, MatterRepository

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
    resolved_signers = _resolve_signers(matter, signers)
    subject = str(email_subject or "").strip() or _default_subject(matter)

    docusign_client = _client(client, client_factory, owner_user_id)
    try:
        created = docusign_client.create_envelope(
            document_bytes,
            document_filename,
            resolved_signers,
            signing_order=signing_order if signing_order in docusign_integration.SIGNING_ORDERS else DEFAULT_SIGNING_ORDER,
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
        "signing_order": signing_order,
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
) -> SignatureStatusResult:
    """Fetch the envelope status; on completion store the signed artifact.

    Reads the persisted envelope id, fetches the live status, and updates the
    stored status. When DocuSign reports ``completed`` it downloads the executed
    combined PDF, registers it as the matter's ``signed`` artifact (via
    :mod:`lifecycle_signed`, eager + best-effort), and flips the matter to
    fully-signed (``executed`` / ``executed_at`` — the markers
    :func:`workflow._is_executed` reads). Idempotent: re-syncing a completed
    matter re-points to the same signed artifact without duplicating it.

    On the same ``completed`` transition it ALSO archives the executed matter to
    Google Drive (the signed PDF + a fresh ``metadata/matter_summary.json``) via
    :func:`_archive_to_drive`. That archive is strictly best-effort: a Drive
    outage / not-connected / any error is swallowed and logged, never blocking the
    executed transition (the envelope IS done at DocuSign). ``drive_sync`` is
    injectable for tests; it defaults to the live Drive archiver.
    """
    repository = repository or DiskMatterRepository()
    matter = _load_matter(matter, matter_id, owner_user_id, repository)

    signature = matter.get(SIGNATURE_FIELD)
    if not isinstance(signature, dict) or not signature.get("envelope_id"):
        raise DocuSignWorkflowError("Matter has no DocuSign envelope to sync.")
    envelope_id = str(signature.get("envelope_id") or "")

    docusign_client = _client(client, client_factory, owner_user_id)
    try:
        status = str(docusign_client.get_envelope_status(envelope_id) or "")
    except DocuSignNotConnectedError:
        raise
    except DocuSignError as error:
        raise DocuSignWorkflowError(str(error)) from error

    completed = status == STATUS_COMPLETED
    signed_artifact_id = ""
    fields: dict[str, Any] = {SIGNATURE_FIELD: {**signature, "status": status, "last_synced_at": _now_iso()}}

    if completed:
        signed_artifact_id = _capture_executed_document(
            docusign_client,
            envelope_id,
            matter,
            matter_id,
            owner_user_id,
            repository,
            signature,
        )
        fields["awaiting_signature"] = False
        fields["executed"] = True
        fields["executed_at"] = _now_iso()
        fields["status"] = "fully_signed"
        if signed_artifact_id:
            fields[SIGNATURE_FIELD]["signed_artifact_id"] = signed_artifact_id

    updated = repository.update_matter_fields(matter_id, fields, owner_user_id=owner_user_id)
    if updated is None:
        updated = {**matter, **fields}

    if completed:
        # Best-effort: archive the fully-executed matter (signed PDF + refreshed
        # matter_summary.json) to its Drive folder. Runs AFTER the matter is
        # persisted so the signed artifact + executed fields are in the registry
        # that sync_matter_folder reads. A Drive outage / not-connected / any
        # error is swallowed inside the archiver and NEVER touches the executed
        # transition above.
        archiver = drive_sync or _archive_to_drive
        archiver(
            matter=updated,
            matter_id=matter_id,
            owner_user_id=owner_user_id,
            repository=repository,
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
            raise NoSignableDocumentError("Matter has no finalized NDA document to send for signature.")
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
# Signer resolution
# ---------------------------------------------------------------------------
def _resolve_signers(matter: dict[str, Any], override: Any | None) -> list[Signer]:
    """Derive the envelope's recipients.

    When ``override`` is supplied (a non-empty list of {name,email[,anchor,role]})
    it is used verbatim. Otherwise we build the two-party signer set: the
    counterparty contact (from the matter's reply/sender + derived counterparty
    name) and the Aspora signatory (from the entity registry bundle, when one is
    selected on the matter). Each side is parallel (any order) by default.

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
        return docusign_integration.normalize_signers(override)

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
            "({name, email}) to send this matter for signature."
        )

    if generated:
        for signer in signers:
            anchor = _ANCHOR_FOR_ROLE.get(signer.get("role") or "")
            if anchor:
                signer["anchor"] = anchor

    return docusign_integration.normalize_signers(signers)


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
) -> None:
    """Mirror the fully-executed matter into its Google Drive folder (best-effort).

    Fires on the DocuSign ``completed`` transition so the executed PDF + a fresh
    ``metadata/matter_summary.json`` land in ``{root}/{counterparty}/{matter}/``.
    Mirrors the intake auto-sync (:meth:`matter_lifecycle._perform_drive_sync`):
    it is gated on Drive being connected AND auto-intake being enabled, then calls
    :func:`drive_integration.sync_matter_folder` and stamps the resulting ``drive``
    pointer back onto the matter.

    STRICTLY best-effort: every failure path — Drive not connected, auto-intake
    off, a settings read blowing up, the sync raising, the write-back failing — is
    swallowed here and recorded via telemetry. This function must NEVER raise, so a
    Drive outage can never block or fail the "completed"/executed transition that
    already persisted before we were called.
    """
    from . import app_settings, drive_integration, telemetry

    try:
        connected = drive_integration.drive_connected(owner_user_id)
        auto_intake = app_settings.drive_auto_intake_enabled()
    except Exception:
        telemetry.increment("drive_oncomplete_skipped")
        return
    if not connected or not auto_intake:
        telemetry.increment("drive_oncomplete_skipped")
        return

    try:
        root_folder_id = str(app_settings.drive_settings().get("folder_id") or "")
    except Exception:
        root_folder_id = ""

    try:
        synced_at = _now_iso()
        synced = drive_integration.sync_matter_folder(
            matter=matter,
            matter_id=matter_id,
            owner_user_id=owner_user_id,
            root_folder_id=root_folder_id,
            synced_at=synced_at,
        )
        repository.update_matter_fields(
            matter_id,
            {
                "drive": {
                    "matter_folder_id": synced["matter_folder_id"],
                    "matter_folder_url": synced["matter_folder_url"],
                    "synced_at": synced_at,
                    "artifacts": synced["artifacts"],
                }
            },
            owner_user_id=owner_user_id,
        )
        telemetry.increment("drive_oncomplete_synced")
        telemetry.increment("drive_files_synced", amount=int(synced.get("synced_count") or 0))
    except Exception:
        telemetry.increment("drive_oncomplete_failed")


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
        raise MatterNotFoundError("Matter not found.")
    return loaded


def _default_subject(matter: dict[str, Any]) -> str:
    counterparty = artifact_registry.derive_counterparty(matter)
    title = str(matter.get("document_title") or matter.get("subject") or "NDA").strip() or "NDA"
    if counterparty and counterparty.lower() != "unknown counterparty":
        return f"Please sign: {title} — {counterparty}"
    return f"Please sign: {title}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
