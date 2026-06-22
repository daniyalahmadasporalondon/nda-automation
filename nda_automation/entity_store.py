"""Persistent, writable store for the signing-entity registry.

Historically the signing entities lived as a frozen, hardcoded list in
:mod:`nda_automation.entity_registry`. This module makes the registry
*authorable* and *durable*: the entity bundles live in a JSON file under
``$NDA_DATA_DIR`` (the same persistent disk the playbook uses), seeded once from
the hardcoded defaults so nothing breaks on first run, and rewritten atomically
whenever an admin saves a change in the Entities console.

Design mirrors the playbook runtime's durability discipline (atomic temp-file +
``os.replace`` + parent fsync, an exclusive flock so concurrent writers serialize,
and a fail-safe fallback to the in-repo defaults when the disk is unreadable).
The store is deliberately simpler than the playbook's full draft/publish/history
machinery: entities are a small operator-managed lookup table, so a single
publish-style atomic save with a stored snapshot is sufficient.

The ``$NDA_DATA_DIR`` path resolution intentionally matches
``checker._resolve_playbook_path`` so that, in a deployment, ``entities.json``
sits beside ``playbook.json`` on the persistent disk and survives a redeploy. In
dev (no ``NDA_DATA_DIR``) reads/writes stay on an in-repo ``data/entities.json``
created on demand, never touching the bundled defaults.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checker import ROOT
from .durable_io import fsync_parent_directory

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None


ENTITY_STORE_VERSION = 1

# The single in-process lock that serializes store writers within a process; the
# flock layered on top serializes across processes (same belt-and-suspenders the
# playbook runtime uses).
_ENTITY_LOCK = threading.RLock()


def _resolve_entity_store_path() -> Path:
    """Resolve the live ``entities.json`` path, persistent when NDA_DATA_DIR is set.

    In a deployment (``NDA_DATA_DIR`` set) the store lives on the persistent disk
    beside ``playbook.json`` so saves survive a redeploy. In dev it lives at
    ``<repo>/data/entities.json``. The directory is created lazily by the writer;
    this only computes the path.
    """
    data_dir = os.environ.get("NDA_DATA_DIR")
    if data_dir:
        return Path(data_dir).expanduser() / "entities.json"
    return ROOT / "data" / "entities.json"


ENTITY_STORE_PATH = _resolve_entity_store_path()


@contextmanager
def locked_entity_store(store_path: Path = ENTITY_STORE_PATH):
    """Hold the in-process lock + an exclusive file lock for the duration."""
    with _ENTITY_LOCK:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = store_path.with_suffix(f"{store_path.suffix}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_json_atomically(value: object, path: Path, *, replace_file=os.replace) -> None:
    data = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary_path, path)
        fsync_parent_directory(path)
    except OSError:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_stored_entities(store_path: Path) -> list[dict[str, Any]] | None:
    """Read the persisted entity list, or ``None`` when absent/corrupt.

    ``None`` (rather than ``[]``) signals "no usable store", so the caller seeds
    from the hardcoded defaults rather than presenting an empty registry.
    """
    try:
        with store_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None
    entities = payload.get("entities") if isinstance(payload, dict) else None
    if not isinstance(entities, list):
        return None
    cleaned = [deepcopy(entity) for entity in entities if isinstance(entity, dict)]
    # An explicitly-empty store is still a valid operator state (every entity
    # removed); only a missing/corrupt store falls back to the defaults.
    return cleaned


def load_entities(
    *,
    defaults: list[dict[str, Any]],
    store_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the live entity bundles, seeding the store from ``defaults`` once.

    First run (no store on disk): the defaults are written through to the store
    so subsequent reads are durable, and the defaults are returned. If the disk
    is unwritable the defaults are still returned (reads never crash), they just
    are not yet persisted. Thereafter the persisted snapshot is authoritative —
    even a redeploy with new bundled defaults does not clobber a saved store.
    """
    if store_path is None:
        store_path = ENTITY_STORE_PATH
    with locked_entity_store(store_path):
        stored = _read_stored_entities(store_path)
        if stored is not None:
            return stored
        seed = [deepcopy(entity) for entity in defaults]
        try:
            _write_snapshot(seed, store_path=store_path, actor="system", source="seed")
        except OSError:
            # Persistent disk unwritable: serve the defaults un-persisted rather
            # than crash. Mirrors checker._resolve_playbook_path's fallback.
            pass
        return seed


class StaleEntityStoreError(Exception):
    """Raised when a compare-and-swap save loses to a concurrent writer.

    Carries the etag the store actually holds so the caller can tell the editor
    its snapshot is stale and it must reload before retrying.
    """

    def __init__(self, current_etag: str) -> None:
        super().__init__(
            "The signing-entity registry changed since this editor loaded it; "
            "the save was rejected to avoid clobbering the other change."
        )
        self.current_etag = current_etag


def compute_etag(entities: list[dict[str, Any]] | None) -> str:
    """Return a stable content hash for an entity list (the optimistic-lock token).

    The hash is over the canonical JSON of the entity list ONLY (not the snapshot
    envelope's ``updated_at``/``updated_by``), so the etag changes exactly when the
    entity content changes — two saves that produce byte-identical entities share an
    etag and don't spuriously collide. ``None`` (no usable store) hashes the same as
    an empty list so a first-run editor and an empty store agree.
    """
    canonical = json.dumps(
        entities or [], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stored_entities_etag(store_path: Path | None = None) -> str:
    """Return the etag of the CURRENTLY-STORED registry (under the store lock).

    Used by the read path so the editor receives the token it must echo back on
    save. Falls back to the empty-list etag when no store exists yet.
    """
    if store_path is None:
        store_path = ENTITY_STORE_PATH
    with locked_entity_store(store_path):
        return compute_etag(_read_stored_entities(store_path))


def save_entities(
    entities: list[dict[str, Any]],
    *,
    store_path: Path | None = None,
    actor: str = "admin",
    expected_etag: str | None = None,
) -> list[dict[str, Any]]:
    """Atomically persist ``entities`` as the new live registry snapshot.

    The caller is responsible for validating ``entities`` first (see
    :mod:`nda_automation.entity_authoring`); this layer owns only durability.
    Returns the stored entities (a deep copy) so the caller cannot mutate the
    on-disk snapshot through the returned reference.

    Optimistic concurrency: when ``expected_etag`` is provided, the CURRENTLY-stored
    entities are re-read and hashed UNDER THE SAME LOCK as the write. If that etag no
    longer matches ``expected_etag``, a concurrent editor wrote since this caller
    loaded its snapshot, so the save is rejected with :class:`StaleEntityStoreError`
    rather than blindly whole-file-replacing (and silently reverting the other edit).
    ``expected_etag=None`` keeps the legacy unconditional behaviour for callers that
    don't supply a token.
    """
    if store_path is None:
        store_path = ENTITY_STORE_PATH
    with locked_entity_store(store_path):
        if expected_etag is not None:
            current_etag = compute_etag(_read_stored_entities(store_path))
            if current_etag != expected_etag:
                raise StaleEntityStoreError(current_etag)
        snapshot = [deepcopy(entity) for entity in entities]
        _write_snapshot(snapshot, store_path=store_path, actor=actor, source="save")
        return [deepcopy(entity) for entity in snapshot]


# The exact placeholder strings the seed ships for an unassigned signer. Only a
# value equal to one of these (or empty) is treated as "not yet filled" and is
# therefore eligible to be filled by the one-time migration below. A real,
# admin-entered value (anything else) is NEVER overwritten.
_SIGNATORY_PLACEHOLDERS: frozenset[str] = frozenset(
    {"", "[Authorised Signatory]", "[Title]"}
)


def _is_placeholder_signatory(value: object) -> bool:
    """True when ``value`` is empty or the exact placeholder (safe to fill)."""
    return str(value or "").strip() in _SIGNATORY_PLACEHOLDERS


# One-time DATA migration: the signatory names live HERE, as data carried by the
# fill itself -- NOT as code-seed defaults. The seed
# (DEFAULT_SIGNING_ENTITIES) stays generic ([Authorised Signatory]/[Title]) so the
# signatories remain ordinary, editable registry values. This mapping seeds the
# initial real signers into the PERSISTENT registry once; thereafter an admin
# owns them via the Entities console. Because the fill only ever touches a literal
# placeholder/empty value, editing one of these in the UI and saving a real value
# makes it permanent -- a later migration run sees a non-placeholder and leaves it.
_SIGNATORY_FILL_BY_ID: dict[str, dict[str, str]] = {
    "aspora_technology": {"name": "Parth Pramendra Garg", "title": "Authorised Signatory"},
    "aspora_financial_services": {"name": "Rahul Bakshi", "title": "Authorised Signatory"},
    "vance_money": {"name": "Rahul Bakshi", "title": "Director"},
}


def migrate_signatory_fills(
    *,
    fills: dict[str, dict[str, str]] | None = None,
    store_path: Path | None = None,
) -> int:
    """One-time, idempotent fill of named entities' signatories in the persisted store.

    For each ``(entity_id -> {name, title})`` in the migration mapping
    (:data:`_SIGNATORY_FILL_BY_ID` by default), set the matching PERSISTED
    entity's ``signatory.name`` / ``.title`` ONLY IF the current value is still
    the exact placeholder (``[Authorised Signatory]`` / ``[Title]``) or empty.
    The names live in the mapping (data), not in the code-seed defaults, so the
    seed stays generic and the filled values are ordinary editable registry data.

    Safety / editability contract:

    * It ONLY replaces a placeholder/empty value. A real, admin-entered signatory
      (any non-placeholder string) is left untouched -- so once an admin edits a
      signatory in the Entities console and saves, a later migration run sees a
      non-placeholder and NEVER reverts it.
    * Field-level: a real name with a placeholder title fills only the title.
    * Idempotent: once filled, a second run finds no placeholder for that field
      and changes nothing (returns 0).
    * No persisted store yet (first run) or an empty operator state: nothing to
      migrate (load_entities seeds the generic defaults on first read).
    * Entities not named in the mapping are never touched (they stay placeholder).
    * Fail-safe: any error is swallowed and 0 returned; this must never crash boot.

    Returns the number of entities whose signatory was filled (0 when every named
    entity already carries a real value / the store is absent / unreadable).
    """
    if store_path is None:
        store_path = ENTITY_STORE_PATH
    if fills is None:
        fills = _SIGNATORY_FILL_BY_ID
    try:
        with locked_entity_store(store_path):
            stored = _read_stored_entities(store_path)
            if not stored:
                # No persisted store (first run) or an empty operator state:
                # nothing to migrate. load_entities() seeds the generic defaults.
                return 0

            changed = 0
            for entity in stored:
                if not isinstance(entity, dict):
                    continue
                target = fills.get(str(entity.get("id") or ""))
                if not target:
                    # Not in the migration mapping -> leave untouched (stays
                    # whatever it is, e.g. the generic placeholder).
                    continue
                signatory = entity.get("signatory")
                if not isinstance(signatory, dict):
                    signatory = {}
                entity_changed = False
                for key in ("name", "title"):
                    fill_value = str(target.get(key) or "").strip()
                    if not fill_value:
                        continue
                    # Fill ONLY when the persisted value is still the placeholder
                    # or empty; a real admin value is never overwritten.
                    if _is_placeholder_signatory(signatory.get(key)):
                        if signatory.get(key) != fill_value:
                            signatory[key] = fill_value
                            entity_changed = True
                if entity_changed:
                    entity["signatory"] = signatory
                    changed += 1

            if changed:
                _write_snapshot(
                    stored, store_path=store_path, actor="system", source="migration"
                )
            return changed
    except Exception:  # noqa: BLE001 - migration is best-effort; never crash boot.
        return 0


def _write_snapshot(
    entities: list[dict[str, Any]],
    *,
    store_path: Path,
    actor: str,
    source: str,
    replace_file=os.replace,
) -> None:
    payload = {
        "version": ENTITY_STORE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": str(actor or "system")[:80] or "system",
        "source": str(source or "save")[:40] or "save",
        "entities": entities,
    }
    _write_json_atomically(payload, store_path, replace_file=replace_file)


__all__ = [
    "ENTITY_STORE_PATH",
    "ENTITY_STORE_VERSION",
    "StaleEntityStoreError",
    "compute_etag",
    "stored_entities_etag",
    "locked_entity_store",
    "load_entities",
    "save_entities",
    "migrate_signatory_fills",
]
