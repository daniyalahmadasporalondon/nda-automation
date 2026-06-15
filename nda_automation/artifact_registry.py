"""The artifact registry — a thin, additive metadata layer over matter documents.

Every document attached to a matter (the original NDA, an AI redline, a reviewed
DOCX, and later generated NDAs + counterparty versions) becomes a tracked
*artifact* with provenance: who produced it (``actor``), what it is (``role``),
which version it is (``version``), and which earlier artifact it was derived from
(``based_on_artifact_id``). A matter carries an ordered ``artifacts`` list plus a
``current_artifact_id`` pointer — "the version that matters now".

This module is deliberately minimal. It does NOT replace the existing document
handling in ``matter_store`` / the redline + export services; it layers a
registry on top of them. The bytes still live in the existing local document
storage — the registry only adds the metadata record and an auto-naming grammar.

The functions here are *pure*: they take a matter dict (and the new artifact's
facts) and return updated ``artifacts`` / ``current_artifact_id`` values. The
seam that persists those values, and that stores/reads the underlying bytes,
lives behind ``MatterRepository`` (so a Drive-backed store can swap in later
without touching this grammar).
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- vocabulary ------------------------------------------------------------
# ``source`` — where the bytes came from.
SOURCE_GMAIL = "gmail"
SOURCE_UPLOAD = "upload"
SOURCE_GENERATED = "generated"
SOURCES = (SOURCE_GMAIL, SOURCE_UPLOAD, SOURCE_GENERATED)

# ``role`` — what the document is in the matter's lifecycle.
ROLE_ORIGINAL = "original"
ROLE_REDLINE = "redline"
ROLE_REVIEWED = "reviewed"
ROLE_GENERATED = "generated"
ROLE_COUNTER = "counter"
ROLE_SENT = "sent"
ROLE_SIGNED = "signed"
ROLES = (
    ROLE_ORIGINAL,
    ROLE_REDLINE,
    ROLE_REVIEWED,
    ROLE_GENERATED,
    ROLE_COUNTER,
    ROLE_SENT,
    ROLE_SIGNED,
)

# ``actor`` — who produced it. ``counterparty``/``ai``/``human`` are the common
# cases; any other non-empty entity slug (e.g. an entity id) is also accepted so
# a generated NDA can name the entity that produced it.
ACTOR_COUNTERPARTY = "counterparty"
ACTOR_AI = "ai"
ACTOR_HUMAN = "human"
KNOWN_ACTORS = (ACTOR_COUNTERPARTY, ACTOR_AI, ACTOR_HUMAN)

# ``stage`` — the chronological lifecycle phase a document occupies. This is the
# Drive *naming* vocabulary (one stage per filename), derived from the (role,
# actor) pair via :func:`stage_for`. The lifecycle, in order:
#   received   — inbound counterparty paper (original + counterparty actor)
#   draft      — our outbound generated/own NDA (original/generated + our actor)
#   ai_redline — the AI review output (redline)
#   legal_review — the human-approved doc at the approval gate (reviewed)
#   sent <-> counter — the negotiation LOOP (each repeatable + versioned)
#   signed     — the executed copy (terminal)
STAGE_RECEIVED = "received"
STAGE_DRAFT = "draft"
STAGE_AI_REDLINE = "ai_redline"
STAGE_LEGAL_REVIEW = "legal_review"
STAGE_SENT = "sent"
STAGE_COUNTER = "counter"
STAGE_SIGNED = "signed"
STAGES = (
    STAGE_RECEIVED,
    STAGE_DRAFT,
    STAGE_AI_REDLINE,
    STAGE_LEGAL_REVIEW,
    STAGE_SENT,
    STAGE_COUNTER,
    STAGE_SIGNED,
)
# The REPEATABLE stages carry a ``_v{N}`` suffix from v1; the one-shot stages
# (received, draft, signed) get no version suffix.
VERSIONED_STAGES = frozenset({STAGE_AI_REDLINE, STAGE_LEGAL_REVIEW, STAGE_SENT, STAGE_COUNTER})

ARTIFACTS_FIELD = "artifacts"
CURRENT_ARTIFACT_FIELD = "current_artifact_id"

_MAX_SLUG_LENGTH = 48
_DEFAULT_EXTENSION = "docx"
_ALLOWED_EXTENSIONS = ("docx", "pdf")


class ArtifactRegistryError(ValueError):
    """A registry operation was given inconsistent or invalid input."""


@dataclass(frozen=True)
class Artifact:
    """One tracked document on a matter, with its provenance.

    ``content_hash`` is the sha256 of the bytes (empty only when bytes are not
    yet available). ``based_on_artifact_id`` records lineage: the artifact this
    one was derived from (e.g. a redline is based on the original; a reviewed
    DOCX is based on the redline). ``stored_filename`` is the key into the
    underlying byte storage — reused as-is for an already-stored original so the
    registry never duplicates existing bytes.
    """

    id: str
    matter_id: str
    source: str
    actor: str
    role: str
    version: int
    name: str
    content_hash: str = ""
    based_on_artifact_id: str = ""
    stored_filename: str = ""
    ext: str = _DEFAULT_EXTENSION
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self.id,
            "matter_id": self.matter_id,
            "source": self.source,
            "actor": self.actor,
            "role": self.role,
            "version": self.version,
            "name": self.name,
            "content_hash": self.content_hash,
            "based_on_artifact_id": self.based_on_artifact_id,
            "stored_filename": self.stored_filename,
            "ext": self.ext,
            "created_at": self.created_at,
        }
        if self.metadata:
            record["metadata"] = dict(self.metadata)
        return record

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "Artifact":
        metadata = record.get("metadata")
        return cls(
            id=str(record.get("id") or ""),
            matter_id=str(record.get("matter_id") or ""),
            source=str(record.get("source") or ""),
            actor=str(record.get("actor") or ""),
            role=str(record.get("role") or ""),
            version=_coerce_version(record.get("version")),
            name=str(record.get("name") or ""),
            content_hash=str(record.get("content_hash") or ""),
            based_on_artifact_id=str(record.get("based_on_artifact_id") or ""),
            stored_filename=str(record.get("stored_filename") or ""),
            ext=str(record.get("ext") or _DEFAULT_EXTENSION),
            created_at=str(record.get("created_at") or ""),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )


# --- reading a matter's registry ------------------------------------------
def matter_artifacts(matter: dict[str, Any]) -> list[Artifact]:
    """The matter's tracked artifacts, in registration order."""
    raw = matter.get(ARTIFACTS_FIELD)
    if not isinstance(raw, list):
        return []
    return [Artifact.from_dict(item) for item in raw if isinstance(item, dict)]


def find_artifact(matter: dict[str, Any], artifact_id: str) -> Artifact | None:
    target = str(artifact_id or "")
    if not target:
        return None
    for artifact in matter_artifacts(matter):
        if artifact.id == target:
            return artifact
    return None


def latest_artifact_for_role(matter: dict[str, Any], role: str) -> Artifact | None:
    """The highest-version artifact with the given role, or None."""
    candidates = [artifact for artifact in matter_artifacts(matter) if artifact.role == role]
    if not candidates:
        return None
    return max(candidates, key=lambda artifact: (artifact.version, artifact.created_at))


# --- the naming grammar ----------------------------------------------------
def stage_for(role: str, actor: str = "") -> str:
    """Map a ``(role, actor)`` pair to its chronological lifecycle ``stage``.

    The stage is the Drive *naming* vocabulary (one stage per filename). The map:
      * ``original`` + counterparty actor -> ``received`` (inbound counterparty paper)
      * ``original``/``generated`` + our actor -> ``draft`` (our outbound NDA)
      * ``redline`` -> ``ai_redline``
      * ``reviewed`` -> ``legal_review``
      * ``sent`` -> ``sent``; ``counter`` -> ``counter``; ``signed`` -> ``signed``

    ``actor`` only matters for the ``original`` role (it decides received vs.
    draft). A non-counterparty actor (``ai``/``human``/an entity slug — i.e. our
    org) on an ``original`` means we authored it, so it reads as a ``draft``.
    """
    role_slug = str(role or "").strip().casefold()
    actor_slug = str(actor or "").strip().casefold()
    if role_slug == ROLE_ORIGINAL:
        return STAGE_RECEIVED if actor_slug == ACTOR_COUNTERPARTY else STAGE_DRAFT
    if role_slug == ROLE_GENERATED:
        return STAGE_DRAFT
    if role_slug == ROLE_REDLINE:
        return STAGE_AI_REDLINE
    if role_slug == ROLE_REVIEWED:
        return STAGE_LEGAL_REVIEW
    if role_slug == ROLE_SENT:
        return STAGE_SENT
    if role_slug == ROLE_COUNTER:
        return STAGE_COUNTER
    if role_slug == ROLE_SIGNED:
        return STAGE_SIGNED
    # Unknown role: fall back to a slug of the role so the name stays meaningful.
    return _slug(role_slug) or "doc"


def stage_is_versioned(stage: str) -> bool:
    """Whether ``stage`` is a repeatable stage that carries a ``_v{N}`` suffix."""
    return str(stage or "").strip().casefold() in VERSIONED_STAGES


def artifact_name(sequence: int, actor: str, role: str, version: int, ext: str) -> str:
    """Build the lifecycle filename ``{NN}_{stage}[_v{N}].{ext}``.

    ``NN`` is the 1-based chronological position in the matter's registration
    order, zero-padded to two digits. ``stage`` is derived from ``(role, actor)``
    via :func:`stage_for`. The repeatable stages (ai_redline, legal_review, sent,
    counter) carry a ``_v{N}`` suffix from v1; the one-shot stages (received,
    draft, signed) get no version suffix. ``ext`` is normalised to a known
    document extension. e.g. ``01_received.docx``, ``02_ai_redline_v1.docx``,
    ``08_signed.pdf``.
    """
    stage = stage_for(role, actor)
    return stage_filename(sequence, stage, version, ext)


def stage_filename(sequence: int, stage: str, version: int, ext: str) -> str:
    """Build ``{NN}_{stage}[_v{N}].{ext}`` from an already-resolved ``stage``.

    The ``_v{N}`` suffix is appended only for the repeatable
    (:data:`VERSIONED_STAGES`) stages. ``stage`` is slugified so the name is
    filesystem-safe.
    """
    sequence_label = f"{max(int(sequence), 0):02d}"
    stage_slug = _stage_slug(stage) or "doc"
    body = f"{sequence_label}_{stage_slug}"
    if stage_is_versioned(stage):
        body = f"{body}_v{max(int(version), 1)}"
    return f"{body}.{_normalise_ext(ext)}"


def next_version_for_role(matter: dict[str, Any], role: str) -> int:
    """The next version number for ``role`` — one past the highest existing one."""
    versions = [artifact.version for artifact in matter_artifacts(matter) if artifact.role == role]
    return (max(versions) + 1) if versions else 1


# --- registering an artifact ----------------------------------------------
def register_artifact(
    matter: dict[str, Any],
    *,
    source: str,
    actor: str,
    role: str,
    content_hash: str = "",
    document_bytes: bytes | None = None,
    based_on_artifact_id: str = "",
    stored_filename: str = "",
    ext: str = "",
    version: int | None = None,
    make_current: bool = True,
    created_at: str = "",
    metadata: dict[str, Any] | None = None,
) -> tuple[Artifact, list[dict[str, Any]], str]:
    """Compute a new artifact record for ``matter`` (no persistence here).

    Returns ``(artifact, artifacts_list, current_artifact_id)`` — the new
    artifact, the full updated ``artifacts`` list to persist, and the (possibly
    updated) ``current_artifact_id`` pointer. The caller (the repository seam)
    stores the bytes and writes these two fields back onto the matter.

    Provenance is validated: a ``based_on_artifact_id`` must reference an
    artifact already on the matter, so lineage never dangles. ``version``
    defaults to one past the highest existing version for the role.
    """
    matter_id = str(matter.get("id") or "")
    source = _validate_choice(source, SOURCES, "source")
    role = _validate_choice(role, ROLES, "role")
    actor = _clean_actor(actor)

    if based_on_artifact_id:
        based_on_artifact_id = str(based_on_artifact_id)
        if find_artifact(matter, based_on_artifact_id) is None:
            raise ArtifactRegistryError(
                f"based_on_artifact_id {based_on_artifact_id!r} is not an artifact of this matter."
            )

    if content_hash:
        content_hash = str(content_hash)
    elif document_bytes is not None:
        content_hash = hash_bytes(document_bytes)

    resolved_version = version if version is not None else next_version_for_role(matter, role)
    if resolved_version < 1:
        raise ArtifactRegistryError("version must be a positive integer.")

    existing = matter_artifacts(matter)
    sequence = len(existing) + 1
    resolved_ext = _normalise_ext(ext or _ext_from_filename(stored_filename) or _DEFAULT_EXTENSION)
    artifact = Artifact(
        id=f"artifact_{uuid.uuid4().hex[:12]}",
        matter_id=matter_id,
        source=source,
        actor=actor,
        role=role,
        version=resolved_version,
        name=artifact_name(sequence, actor, role, resolved_version, resolved_ext),
        content_hash=content_hash,
        based_on_artifact_id=based_on_artifact_id,
        stored_filename=str(stored_filename or ""),
        ext=resolved_ext,
        created_at=created_at or _now(),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )

    artifacts_list = [item.to_dict() for item in existing]
    artifacts_list.append(artifact.to_dict())
    current_artifact_id = (
        artifact.id if make_current else str(matter.get(CURRENT_ARTIFACT_FIELD) or "")
    )
    return artifact, artifacts_list, current_artifact_id


def set_current_artifact(matter: dict[str, Any], artifact_id: str) -> str:
    """Validate and return the ``current_artifact_id`` to persist.

    Raises if ``artifact_id`` is not an artifact of the matter so the pointer
    can never go dangling.
    """
    artifact_id = str(artifact_id or "")
    if not artifact_id or find_artifact(matter, artifact_id) is None:
        raise ArtifactRegistryError(f"{artifact_id!r} is not an artifact of this matter.")
    return artifact_id


# --- counterparty derivation ----------------------------------------------
# The single source of truth for "who is the counterparty on this matter?". It
# lives here (a leaf module that already owns artifact metadata) so both the
# Drive filing layer and the UI view read the SAME best-available name without an
# import cycle. ``drive_integration`` re-exports ``derive_counterparty`` for
# backward compatibility; ``matter_view`` imports it for the public_matter field.
COUNTERPARTY_UNKNOWN = "Unknown Counterparty"


def derive_counterparty(matter: dict[str, Any]) -> str:
    """Best-available counterparty name for a matter, display/Drive-safe.

    Preference:
      1. a generated NDA's manifest ``counterparty_name`` (exact, stored on the
         generated artifact's metadata);
      2. a VERIFIED AI-extracted counterparty (from the contract preamble, stored
         on ``intake_metadata['counterparty']``);
      3. the matter's email ``subject`` run through the deterministic
         :func:`~nda_automation.counterparty_naming.normalize_counterparty`
         fallback (strips Fwd/Re prefixes, drops the first-party side of an
         ``A <> Aspora`` subject, etc.);
      4. ``"Unknown Counterparty"``.

    The chosen name is sanitised (control chars and path separators stripped) but
    kept human-readable (spaces preserved). Both the manifest name and the AI
    value BYPASS normalization — they are already exact. ``normalize_counterparty``
    runs BEFORE ``_counterparty_safe_name``, so a residual ``/`` it keeps as a
    connector is still a real ``/`` at split time and the sanitizer converts any
    surviving ``/`` to a space afterward. (If a future refactor sanitizes the
    subject first, the slash-as-connector rule silently dies — see the
    'Fwd: Stark Industries / Aspora' regression test.)

    For inbound matters this is a best-effort name, not an exact legal entity —
    callers should present it as-is and not imply false precision.
    """
    # Local import keeps the leaf module dependency one-directional and avoids any
    # import cycle through entity_registry.
    from .counterparty_naming import normalize_counterparty

    manifest_name = counterparty_from_generation(matter)
    review_name = counterparty_from_review(matter)
    candidate = (
        manifest_name
        or review_name
        or normalize_counterparty(str(matter.get("subject") or ""))
    )
    cleaned = _counterparty_safe_name(candidate)
    return cleaned or COUNTERPARTY_UNKNOWN


def counterparty_from_review(matter: dict[str, Any]) -> str:
    """The AI-extracted, VERIFIED counterparty name stored on the matter, else ``""``.

    This is the SINGLE place that knows the storage location. Per the shared
    contract the AI value lives at ``matter['intake_metadata']['counterparty']`` as
    a dict ``{"name", "confidence", "verified", "first_party", "second_party",
    "source"}``. Read DEFENSIVELY — the matter may lack the key entirely,
    ``intake_metadata`` may be absent or not a dict, and the value may not be a
    dict.

    Returns the name ONLY when ``verified`` is true, or — when ``verified`` is
    absent — when ``confidence`` >= 0.75. Otherwise ``""`` so the caller falls
    through to the deterministic subject normalizer.
    """
    intake = matter.get("intake_metadata")
    if not isinstance(intake, dict):
        return ""
    record = intake.get("counterparty")
    if not isinstance(record, dict):
        return ""
    name = str(record.get("name") or "").strip()
    if not name:
        return ""
    if "verified" in record:
        return name if bool(record.get("verified")) else ""
    try:
        confidence = float(record.get("confidence"))
    except (TypeError, ValueError):
        return ""
    return name if confidence >= 0.75 else ""


def counterparty_from_generation(matter: dict[str, Any]) -> str:
    """Pull the counterparty company name from a generated artifact's manifest.

    Generated NDAs stash the generation manifest on the artifact's
    ``metadata['generation']`` (the matter-level intake_metadata drops unknown
    keys, so the artifact metadata is the reliable source). Returns the manifest's
    ``counterparty_name`` when present.
    """
    for artifact in matter_artifacts(matter):
        metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
        generation = metadata.get("generation")
        if isinstance(generation, dict):
            name = str(generation.get("counterparty_name") or "").strip()
            if name:
                return name
    return ""


def _counterparty_safe_name(value: object) -> str:
    """Sanitise a counterparty display name: strip control chars + path separators.

    Keeps the name human-readable (spaces and most punctuation preserved) but
    removes characters that break a Drive path or a local mirror: slashes,
    backslashes, NUL/control characters. Collapses whitespace and trims.
    """
    text = str(value or "")
    cleaned = []
    for ch in text:
        if ch in ("/", "\\", "\x00") or ord(ch) < 32:
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    return " ".join("".join(cleaned).split()).strip()


# --- helpers ---------------------------------------------------------------
def hash_bytes(document_bytes: bytes) -> str:
    return hashlib.sha256(document_bytes).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: object) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().casefold()).strip("-")
    return slug[:_MAX_SLUG_LENGTH].strip("-")


def _stage_slug(value: object) -> str:
    """Filesystem-safe slug for a lifecycle stage, PRESERVING underscores.

    Unlike :func:`_slug`, the stage vocabulary keeps ``_`` as a word separator so
    multi-word stages (``ai_redline``, ``legal_review``) survive intact. Any other
    non-alphanumeric run still collapses to ``_``.
    """
    slug = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().casefold()).strip("_")
    return slug[:_MAX_SLUG_LENGTH].strip("_")


def _clean_actor(actor: object) -> str:
    slug = _slug(actor)
    if not slug:
        raise ArtifactRegistryError("actor is required.")
    return slug


def _validate_choice(value: object, allowed: tuple[str, ...], label: str) -> str:
    candidate = str(value or "").strip().casefold()
    if candidate not in allowed:
        raise ArtifactRegistryError(
            f"{label} must be one of {', '.join(allowed)}; got {value!r}."
        )
    return candidate


def _normalise_ext(ext: object) -> str:
    candidate = str(ext or "").strip().lstrip(".").casefold()
    if candidate in _ALLOWED_EXTENSIONS:
        return candidate
    return _DEFAULT_EXTENSION


def _ext_from_filename(filename: object) -> str:
    suffix = Path(str(filename or "")).suffix.lstrip(".").casefold()
    return suffix if suffix in _ALLOWED_EXTENSIONS else ""


def _coerce_version(value: object) -> int:
    try:
        version = int(value)
    except (TypeError, ValueError):
        return 1
    return version if version >= 1 else 1
