"""Bounded, content-addressed disk cache for composed reviewed-DOCX bytes.

The reviewed-DOCX pipeline (build_reviewed_docx -> build_matter_redline -> body
re-extraction -> XML composition -> open-health x2 -> coverage gate, then
accept_all_revisions for the accepted view and EMF/WMF image normalization) is
expensive and re-ran on EVERY GET/HEAD of ``/api/matters/<id>/reviewed-docx`` --
for every view mode, for the FE's faithful-fallback fetch, and even for HEAD
(which ran the identical full build with send_body=False).

This module caches the FINAL served bytes (post accept + normalize) keyed on a
fingerprint of everything that affects them: the matter's source text, the
reviewer draft (decisions), the stored review result, the source identity, and
the view mode. Repeat views, the fallback fetch, and HEAD serve from disk; a
change to the text, the draft, the review result, or the mode changes the
fingerprint and misses. The fingerprint doubles as the strong ETag so a
conditional GET (If-None-Match) short-circuits to 304.

The cache reuses document_rendering's eviction bound (MAX_RENDER_CACHE_ENTRIES)
and lives under the same durable cache root, so the whole render/export cache
shares one bounded on-disk budget with LRU-by-mtime eviction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import document_rendering, matter_store

CACHE_DIRNAME = "reviewed-docx"
# Bumped when the composition or fingerprint composition changes so stale entries
# from an older build can never be served.
CACHE_VERSION = "reviewed-docx:v1"

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CachedReviewedDocx:
    data: bytes
    filename: str
    content_type: str
    headers: dict[str, str]


def cache_root() -> Path:
    return (matter_store.DATA_DIR / "cache" / CACHE_DIRNAME).resolve()


def reviewed_docx_fingerprint(
    matter_id: str,
    matter: dict[str, Any],
    *,
    changes_mode: str,
    owner_user_id: str = "",
) -> str:
    """A stable content fingerprint of the composed reviewed-DOCX bytes.

    Folds in every input that changes the output so two GETs with no change hit
    the same entry, while a change to any input misses:

    * ``matter_id`` + owner -- so no two matters ever collide on one entry.
    * ``changes_mode`` (tracked|accepted) -- accepted flattens the revisions, so
      the two modes are distinct entries.
    * ``extracted_text`` -- the matter source text the redline is composed against.
    * the reviewer draft (``reviewed_docx_payload`` -> export/manual redlines +
      review comments derived from the per-clause decisions).
    * the stored ``review_result`` (redline_edits + playbook_version hash + clauses).
    * ``source_filename`` -- a coarse source-identity guard.
    """
    from . import approval  # local import avoids any import cycle at module load

    try:
        draft = approval.reviewed_docx_payload(matter)
    except Exception:  # noqa: BLE001 -- fingerprint must never raise; degrade to a
        # per-request-unique key (cache miss) rather than crash the GET.
        draft = {"_fingerprint_error": True, "_nonce": os.urandom(8).hex()}

    material = {
        "version": CACHE_VERSION,
        "matter_id": str(matter_id),
        "owner": str(owner_user_id or matter.get("owner_user_id") or ""),
        "changes_mode": str(changes_mode),
        "extracted_text": matter.get("extracted_text") or "",
        "source_filename": str(matter.get("source_filename") or ""),
        "draft": draft,
        "review_result": matter.get("review_result"),
    }
    canonical = json.dumps(material, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def etag_for(fingerprint: str) -> str:
    return f'"reviewed-docx-{fingerprint}"'


def _entry_paths(fingerprint: str) -> tuple[Path, Path]:
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("Reviewed-DOCX cache fingerprint must be a sha256 hex digest.")
    root = cache_root()
    data_path = (root / f"{fingerprint}.bin").resolve()
    meta_path = (root / f"{fingerprint}.json").resolve()
    if data_path.parent != root or meta_path.parent != root:
        raise ValueError("Reviewed-DOCX cache path escapes the cache root.")
    return data_path, meta_path


def load(fingerprint: str) -> CachedReviewedDocx | None:
    """Return the cached reviewed DOCX for ``fingerprint``, or None on a miss.

    A hit LRU-touches the entry so the next eviction pass spares it. A half-written
    or unparseable entry is treated as a miss (the caller recomposes).
    """
    try:
        data_path, meta_path = _entry_paths(fingerprint)
    except ValueError:
        return None
    try:
        data = data_path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    headers = meta.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    # LRU touch (both parts) so a fresh hit is not the next thing evicted.
    now = None
    for path in (data_path, meta_path):
        try:
            os.utime(path, now)
        except OSError:
            pass
    return CachedReviewedDocx(
        data=data,
        filename=str(meta.get("filename") or "reviewed.docx"),
        content_type=str(meta.get("content_type") or ""),
        headers={str(k): str(v) for k, v in headers.items()},
    )


def store(
    fingerprint: str,
    data: bytes,
    *,
    filename: str,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> None:
    """Persist the composed bytes + response metadata under ``fingerprint``.

    Best-effort and fail-open: a filesystem error (full/read-only /var/data) must
    never break serving the document the caller already composed.
    """
    try:
        data_path, meta_path = _entry_paths(fingerprint)
    except ValueError:
        return
    meta = {
        "filename": filename,
        "content_type": content_type,
        "headers": {str(k): str(v) for k, v in (headers or {}).items()},
    }
    try:
        data_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(data_path, data)
        _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    except OSError:
        return
    _enforce_bound(keep=data_path)


def invalidate(fingerprint: str) -> None:
    """Remove a specific entry (best-effort)."""
    try:
        data_path, meta_path = _entry_paths(fingerprint)
    except ValueError:
        return
    for path in (data_path, meta_path):
        try:
            path.unlink()
        except OSError:
            pass


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _enforce_bound(*, keep: Path | None = None) -> None:
    """Evict least-recently-used entries so the cache stays within the shared
    MAX_RENDER_CACHE_ENTRIES bound. Recency is the ``.bin`` file's mtime (bumped on
    every hit); both the ``.bin`` and its ``.json`` sidecar are removed together.
    Best-effort: a filesystem error while pruning must never fail the store."""
    root = cache_root()
    max_entries = document_rendering.MAX_RENDER_CACHE_ENTRIES
    try:
        entries = [child for child in root.iterdir() if child.is_file() and child.suffix == ".bin"]
    except OSError:
        return
    if len(entries) <= max_entries:
        return
    keep_resolved = keep.resolve() if keep is not None else None

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    for entry in sorted(entries, key=_mtime):
        if len(entries) <= max_entries:
            break
        if keep_resolved is not None and entry.resolve() == keep_resolved:
            continue
        for path in (entry, entry.with_suffix(".json")):
            try:
                path.unlink()
            except OSError:
                pass
        entries.remove(entry)
