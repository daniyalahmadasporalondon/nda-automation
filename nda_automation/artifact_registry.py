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
ROLES = (ROLE_ORIGINAL, ROLE_REDLINE, ROLE_REVIEWED, ROLE_GENERATED, ROLE_COUNTER)

# ``actor`` — who produced it. ``counterparty``/``ai``/``human`` are the common
# cases; any other non-empty entity slug (e.g. an entity id) is also accepted so
# a generated NDA can name the entity that produced it.
ACTOR_COUNTERPARTY = "counterparty"
ACTOR_AI = "ai"
ACTOR_HUMAN = "human"
KNOWN_ACTORS = (ACTOR_COUNTERPARTY, ACTOR_AI, ACTOR_HUMAN)

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
def artifact_name(sequence: int, actor: str, role: str, version: int, ext: str) -> str:
    """Build ``{sequence}_{actor}_{role}_v{n}.{ext}`` (e.g. ``01_acme_original_v1.docx``).

    ``sequence`` is the 1-based position of the artifact in the matter's
    registration order, zero-padded to two digits. ``actor``/``role`` are
    slugified so the name is filesystem-safe; ``ext`` is normalised to a known
    document extension.
    """
    sequence_label = f"{max(int(sequence), 0):02d}"
    actor_slug = _slug(actor) or "actor"
    role_slug = _slug(role) or "doc"
    version_label = max(int(version), 1)
    return f"{sequence_label}_{actor_slug}_{role_slug}_v{version_label}.{_normalise_ext(ext)}"


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

    Preference: (1) a generated NDA's manifest ``counterparty_name`` (stored on the
    generated artifact's metadata), (2) the matter's cleaned ``subject``, (3)
    ``"Unknown Counterparty"``. The result is sanitised (control chars and path
    separators stripped) but kept human-readable (spaces preserved). For inbound
    matters this is a best-effort name derived from the email subject, not an exact
    legal entity — callers should present it as-is and not imply false precision.
    """
    manifest_name = counterparty_from_generation(matter)
    candidate = manifest_name or str(matter.get("subject") or "").strip()
    cleaned = _counterparty_safe_name(candidate)
    return cleaned or COUNTERPARTY_UNKNOWN


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
