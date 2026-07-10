"""Bounded, content-addressed disk cache for composed reviewed-DOCX bytes.

The reviewed-DOCX pipeline (build_reviewed_docx -> build_matter_redline -> body
re-extraction -> XML composition -> open-health x2 -> coverage gate, then
accept_all_revisions for the accepted view and EMF/WMF image normalization) is
expensive and re-ran on EVERY GET/HEAD of ``/api/matters/<id>/reviewed-docx`` --
for every view mode, for the FE's faithful-fallback fetch, and even for HEAD
(which ran the identical full build with send_body=False).

This module caches the FINAL served bytes (post accept + normalize) keyed on a
fingerprint of everything the composer actually reads:

  * the matter's source text (``extracted_text``),
  * the reviewer draft (per-clause decisions -> ``reviewed_docx_payload``),
  * the stored review result,
  * the view mode (tracked|accepted),
  * the COMPOSITION SOURCE IDENTITY -- the content hash of the exact bytes the
    redline is composed against: the role="working" DOCX's stored content hash
    when a working artifact exists (the composer substitutes it for the source),
    else a hash of the raw source-document bytes. Neither the source bytes nor the
    working artifact are captured by ``extracted_text``/``source_filename`` (those
    are only a PROXY), so without this a rebuilt working DOCX -- e.g. the
    garble_backfill admin tool healing a garbled PDF matter -- would keep serving
    the stale, still-garbled composed bytes off a durable ``/var/data``.

Repeat views, the fallback fetch, and HEAD serve from disk; a change to ANY of
those inputs changes the fingerprint and misses. The fingerprint doubles as the
strong ETag, but a conditional GET only short-circuits to 304 when the cache
ACTUALLY holds the representation (the route checks ``load`` before honoring an
If-None-Match) -- the ETag names inputs, not a produced body, so a fingerprint
match alone never authorizes "Not Modified" for bytes we cannot serve.

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

from . import artifact_registry, document_rendering, matter_store

CACHE_DIRNAME = "reviewed-docx"
# COORDINATION RULE: bump this on ANY change to what the composer produces or to
# what this fingerprint reads -- i.e. the redline composition, the accept-all pass,
# the EMF/WMF image normalization, the DOCX content-coverage gate, OR the set of
# inputs folded into reviewed_docx_fingerprint below. On a durable /var/data a
# pre-change entry survives a deploy, so a stale-composition entry could be served
# after a behavioural change that DID NOT bump this. (Example the gate flagged: a
# sibling branch changed the image normalizer without bumping -- that MUST bump.)
# v2: folded the composition source identity (source/working-artifact content hash)
# into the fingerprint so a rebuilt working DOCX or changed source bytes misses.
CACHE_VERSION = "reviewed-docx:v2"

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CachedReviewedDocx:
    data: bytes
    filename: str
    content_type: str
    headers: dict[str, str]


def cache_root() -> Path:
    return (matter_store.DATA_DIR / "cache" / CACHE_DIRNAME).resolve()


def _composition_source_identity(matter: dict[str, Any]) -> str:
    """The identity of the exact bytes the composer reads for the SOURCE document.

    ``redline_export_service`` composes against the raw source-document bytes --
    UNLESS the matter carries a role="working" DOCX (a PDF reconstructed once at
    ingest), in which case it SUBSTITUTES the working DOCX bytes for the source
    (``_working_docx_for_matter``). So the fingerprint must key on whichever the
    composer will actually use; ``extracted_text``/``source_filename`` are only a
    proxy and do NOT move when those bytes are rebuilt in place (the garble_backfill
    working-DOCX rebuild is the proven case). Returns a short tagged token:

    * ``"working:<content_hash>"`` -- the working artifact's stored sha256 (computed
      at registration; NO disk read, NO rehash). It is byte-derived, so
      re-registering IDENTICAL bytes yields the same token (still a HIT) while a
      rebuilt/different working DOCX yields a new token (a MISS). Falls back to the
      artifact id only if a legacy record somehow lacks a content hash.
    * ``"source:<sha256>"`` -- for a matter with NO working artifact, a hash of the
      raw source bytes the composer reads. This is a single small file read + a
      sha256, negligible beside the multi-second compose this cache exists to skip,
      and runs only for native-DOCX / legacy-PDF matters (no cheaper stored hash is
      guaranteed for the raw source, so we hash it directly for correctness).

    Never raises: any error degrades to an empty token (the other fingerprint
    inputs still apply), never a crash of the GET.
    """
    try:
        working = artifact_registry.latest_artifact_for_role(
            matter, artifact_registry.ROLE_WORKING
        )
        if working is not None:
            return "working:" + (working.content_hash or working.id or "")
        source_bytes = matter_store.get_source_document_bytes(matter)
        if not source_bytes:
            return "source:"
        return "source:" + hashlib.sha256(source_bytes).hexdigest()
    except Exception:  # noqa: BLE001 -- identity is best-effort; a failure must not
        # crash the fingerprint. An empty token is stable (the source_filename +
        # extracted_text proxies still contribute), so it degrades safely.
        return "source:error"


def reviewed_docx_fingerprint(
    matter_id: str,
    matter: dict[str, Any],
    *,
    changes_mode: str,
    owner_user_id: str = "",
) -> str:
    """A stable content fingerprint of the composed reviewed-DOCX bytes.

    Folds in every input the composer reads so two GETs with no change hit the same
    entry, while a change to any input misses:

    * ``matter_id`` + owner -- so no two matters ever collide on one entry.
    * ``changes_mode`` (tracked|accepted) -- accepted flattens the revisions, so
      the two modes are distinct entries.
    * ``extracted_text`` -- the matter source text the redline is composed against.
    * the reviewer draft (``reviewed_docx_payload`` -> export/manual redlines +
      review comments derived from the per-clause decisions).
    * the stored ``review_result`` (redline_edits + playbook_version hash + clauses).
    * ``source_filename`` -- a coarse source-identity guard.
    * ``source_identity`` -- the CONTENT hash of the actual bytes the composer reads
      (the working DOCX's stored hash, or a hash of the raw source bytes). This is
      the load-bearing addition: without it a rebuilt working DOCX or edited source
      keeps the same key and the composer is never re-run (stale bytes served).
      See ``_composition_source_identity``.
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
        "source_identity": _composition_source_identity(matter),
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
