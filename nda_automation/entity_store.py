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
    store_path: Path = ENTITY_STORE_PATH,
) -> list[dict[str, Any]]:
    """Return the live entity bundles, seeding the store from ``defaults`` once.

    First run (no store on disk): the defaults are written through to the store
    so subsequent reads are durable, and the defaults are returned. If the disk
    is unwritable the defaults are still returned (reads never crash), they just
    are not yet persisted. Thereafter the persisted snapshot is authoritative —
    even a redeploy with new bundled defaults does not clobber a saved store.
    """
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


def save_entities(
    entities: list[dict[str, Any]],
    *,
    store_path: Path = ENTITY_STORE_PATH,
    actor: str = "admin",
) -> list[dict[str, Any]]:
    """Atomically persist ``entities`` as the new live registry snapshot.

    The caller is responsible for validating ``entities`` first (see
    :mod:`nda_automation.entity_authoring`); this layer owns only durability.
    Returns the stored entities (a deep copy) so the caller cannot mutate the
    on-disk snapshot through the returned reference.
    """
    with locked_entity_store(store_path):
        snapshot = [deepcopy(entity) for entity in entities]
        _write_snapshot(snapshot, store_path=store_path, actor=actor, source="save")
        return [deepcopy(entity) for entity in snapshot]


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
    "locked_entity_store",
    "load_entities",
    "save_entities",
]
