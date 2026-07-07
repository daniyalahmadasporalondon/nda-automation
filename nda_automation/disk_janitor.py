"""Archive-rotation disk janitor.

Root-cause fix for a production disk-full incident: ``matter_store`` archives
every retention-pruned / bulk-archived matter into ``DATA_DIR / pruned-matters/``
(a byte copy of the source document plus a per-matter archive JSON) as a
recoverability safety-net, and NOTHING ever cleans it up. On the 1 GB Render
disk that archive grew unbounded until the volume filled.

This module reclaims space SAFELY. It ONLY ever deletes inside
``pruned-matters/`` -- never live matter records, ``users.json``,
``matters.lock``, sessions, OAuth tokens, telemetry, or the stored source
documents for LIVE matters. Every delete target is resolved to a real path and
asserted to live strictly inside the archive dir before unlink, so a symlink or
``..`` entry inside the archive can never trick the janitor into escaping it.

Design notes:

* An "archived matter" is a top-level ``<matter_id>.json`` file directly inside
  ``pruned-matters/``. Its (optional) archived source document lives under
  ``pruned-matters/uploads/<name>`` and is recorded on the JSON under
  ``archived_source_document.archive_path``. Deleting an archived matter unlinks
  its JSON and (if present, and provably inside the archive dir) that one
  source document.
* Age is taken from the archive JSON's ``archived_at`` if present, else the
  archive JSON file's mtime.
* Triggers (all default-ON but conservative) and knobs live in env vars; see the
  ``*_ENV`` constants. A retention floor (``NDA_ARCHIVE_KEEP_MIN``, default 20)
  guarantees the newest N archived matters are NEVER deleted, whatever the caps
  say -- recoverability wins ties.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import matter_store, telemetry

# --- env knobs -------------------------------------------------------------

# Cap on the TOTAL size of pruned-matters/. Over this, oldest archived matters
# are dropped (oldest-first) until back under the cap. Default ~250 MB. Set <= 0
# to disable the size trigger.
ARCHIVE_MAX_BYTES_ENV = "NDA_ARCHIVE_MAX_BYTES"
DEFAULT_ARCHIVE_MAX_BYTES = 250 * 1024 * 1024

# Drop archived matters older than N days. Default 30. Set <= 0 to disable the
# age trigger.
ARCHIVE_RETENTION_DAYS_ENV = "NDA_ARCHIVE_RETENTION_DAYS"
DEFAULT_ARCHIVE_RETENTION_DAYS = 30

# Disk high-watermark: when DATA_DIR disk usage is at/over this percent, prune
# aggressively (down toward the size cap even if not otherwise triggered).
# Default 85. Set <= 0 to disable the watermark trigger.
DISK_HIGH_WATERMARK_PCT_ENV = "NDA_DISK_HIGH_WATERMARK_PCT"
DEFAULT_DISK_HIGH_WATERMARK_PCT = 85.0

# Retention floor: never delete below this many newest archived matters, no
# matter what the caps say. Recoverability floor. Default 20.
ARCHIVE_KEEP_MIN_ENV = "NDA_ARCHIVE_KEEP_MIN"
DEFAULT_ARCHIVE_KEEP_MIN = 20

# Minimum seconds between rotation runs (rate-limit for the poll-loop wiring).
# Default 3600 (hourly). Set <= 0 to disable rate-limiting.
ARCHIVE_ROTATION_MIN_INTERVAL_ENV = "NDA_ARCHIVE_ROTATION_MIN_INTERVAL_SECONDS"
DEFAULT_ARCHIVE_ROTATION_MIN_INTERVAL_SECONDS = 3600

# Monotonic timestamp of the last completed rotation attempt (process-local);
# the poll wiring reads this to rate-limit. Module-global so it survives ticks.
_LAST_ROTATION_MONOTONIC = 0.0


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- disk usage ------------------------------------------------------------


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int
    free: int
    percent: float


def disk_usage(path: Path | None = None) -> DiskUsage | None:
    """``shutil.disk_usage`` over DATA_DIR (or ``path``) as used/total/free/percent.

    Returns ``None`` if the read fails (missing dir, permission, platform quirk)
    -- callers MUST treat ``None`` as "unknown" and refuse watermark-driven
    deletion, never as "empty disk".
    """
    target = Path(path) if path is not None else matter_store.DATA_DIR
    try:
        usage = shutil.disk_usage(target)
    except (OSError, ValueError):
        return None
    total = int(usage.total)
    used = int(usage.used)
    free = int(usage.free)
    percent = (used / total * 100.0) if total > 0 else 0.0
    return DiskUsage(total=total, used=used, free=free, percent=percent)


# --- archive introspection -------------------------------------------------


def _archive_dir() -> Path:
    """The real (symlink-resolved) pruned-matters/ dir under the CURRENT DATA_DIR.

    Read live from ``matter_store`` every call so tests (and any DATA_DIR
    re-pointing) are honoured, and resolved so the containment guard below
    compares real paths.
    """
    return (matter_store.DATA_DIR / matter_store.PRUNED_ARCHIVE_DIRNAME).resolve()


def _is_strictly_inside(candidate: Path, root: Path) -> bool:
    """True iff ``candidate`` (resolved) is strictly inside ``root`` (resolved).

    Resolves symlinks/``..`` on both sides, so an archive entry that is a symlink
    pointing at a live file, or that contains ``..``, resolves OUTSIDE the
    archive root and is refused. ``root`` itself is not "inside" itself.
    """
    try:
        real = candidate.resolve()
        real_root = root.resolve()
    except (OSError, RuntimeError):
        return False
    if real == real_root:
        return False
    return real_root in real.parents


@dataclass
class _ArchivedMatter:
    json_path: Path
    matter_id: str
    age_key: float  # smaller == older; sort ascending to delete oldest first
    size_bytes: int
    source_path: Path | None  # archived source doc, if referenced + resolvable


def _archived_at_epoch(record: dict[str, Any]) -> float | None:
    """Best-effort epoch seconds from a record's ``archived_at`` / ``pruned_at``.

    Accepts an ISO-8601 string (with or without trailing ``Z``) or a numeric
    epoch. Returns ``None`` if absent/unparseable so the caller falls back to
    file mtime.
    """
    for key in ("archived_at", "pruned_at"):
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text:
            continue
        try:
            normalized = text.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def _load_archived_matters(archive_dir: Path) -> list[_ArchivedMatter]:
    """Enumerate top-level ``<id>.json`` archive records under ``archive_dir``.

    Skips anything that is not a real regular file directly inside the archive
    dir (symlinks/subdirs/junk are ignored, not deleted here). One unreadable
    entry never aborts enumeration.
    """
    entries: list[_ArchivedMatter] = []
    try:
        raw_children = list(archive_dir.iterdir())
    except OSError:
        return entries

    for child in raw_children:
        try:
            if child.suffix != ".json":
                continue
            # Only genuine regular files strictly inside the archive dir.
            if child.is_symlink() or not child.is_file():
                continue
            if not _is_strictly_inside(child, archive_dir):
                continue

            stat = child.stat()
            record: dict[str, Any] = {}
            try:
                with child.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    record = loaded
            except (OSError, ValueError):
                record = {}

            matter_id = str(record.get("id") or child.stem)
            archived_epoch = _archived_at_epoch(record)
            age_key = archived_epoch if archived_epoch is not None else float(stat.st_mtime)

            total_size = int(stat.st_size)
            source_path: Path | None = None
            source_meta = record.get("archived_source_document")
            if isinstance(source_meta, dict) and source_meta.get("present"):
                rel = str(source_meta.get("archive_path") or "")
                if rel:
                    candidate = archive_dir / rel
                    # Only treat as ours if it resolves strictly inside the
                    # archive dir and is a real file (never a symlink escape).
                    if (
                        not candidate.is_symlink()
                        and candidate.exists()
                        and _is_strictly_inside(candidate, archive_dir)
                    ):
                        source_path = candidate
                        try:
                            total_size += int(candidate.stat().st_size)
                        except OSError:
                            pass

            entries.append(
                _ArchivedMatter(
                    json_path=child,
                    matter_id=matter_id,
                    age_key=age_key,
                    size_bytes=total_size,
                    source_path=source_path,
                )
            )
        except OSError:
            # One bad entry never aborts the enumeration.
            continue

    # Oldest first (ascending age_key) so callers delete from the front.
    entries.sort(key=lambda item: item.age_key)
    return entries


def _safe_unlink(target: Path, archive_dir: Path) -> int:
    """Unlink ``target`` iff it is strictly inside ``archive_dir``; return bytes freed.

    Refuses (returns 0) if the containment guard fails -- the last line of
    defence against deleting anything outside pruned-matters/. Never raises.
    """
    try:
        if not _is_strictly_inside(target, archive_dir):
            return 0
        try:
            size = int(target.stat().st_size)
        except OSError:
            size = 0
        target.unlink()
        return size
    except OSError:
        return 0


# --- rotation --------------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    """One greppable stdout JSON telemetry line. Counts/bytes only, no PII."""
    try:
        print(json.dumps({"event": "disk_janitor", **payload}, separators=(",", ":")))
    except (TypeError, ValueError):
        pass


def run_archive_rotation() -> dict[str, Any]:
    """Trim ``pruned-matters/`` back under its caps, oldest-first, fail-safe.

    Deletes the oldest archived matters (by ``archived_at`` / archive mtime)
    until total archive size is under ``NDA_ARCHIVE_MAX_BYTES`` and no remaining
    entry is older than ``NDA_ARCHIVE_RETENTION_DAYS`` -- but ALWAYS keeps the
    newest ``NDA_ARCHIVE_KEEP_MIN`` archived matters, and NEVER touches anything
    outside the archive dir. When DATA_DIR disk usage is at/over
    ``NDA_DISK_HIGH_WATERMARK_PCT`` it prunes aggressively down to the size cap.

    Returns a summary dict (also emitted as a stdout JSON line). Never raises.
    """
    global _LAST_ROTATION_MONOTONIC
    _LAST_ROTATION_MONOTONIC = time.monotonic()

    summary: dict[str, Any] = {
        "removed": 0,
        "freed_bytes": 0,
        "before_bytes": 0,
        "after_bytes": 0,
        "disk_pct": None,
        "skipped": None,
    }

    # Read usage for telemetry and the watermark decision. A None (failed) read
    # only disables the WATERMARK trigger -- the size/age caps are self-contained
    # and safe. If NOTHING but the watermark would fire, a None read means we do
    # not know how full the disk is, so we do not delete on that basis.
    usage = disk_usage()

    try:
        archive_dir = _archive_dir()
    except (OSError, RuntimeError) as error:
        summary["skipped"] = f"archive_dir_error:{error.__class__.__name__}"
        _emit(summary)
        return summary

    if not archive_dir.exists():
        # Nothing archived yet -- nothing to do (not an error).
        summary["skipped"] = "no_archive_dir"
        if usage is not None:
            summary["disk_pct"] = round(usage.percent, 2)
        _emit(summary)
        return summary

    entries = _load_archived_matters(archive_dir)
    before_bytes = sum(item.size_bytes for item in entries)
    summary["before_bytes"] = before_bytes
    summary["after_bytes"] = before_bytes
    if usage is not None:
        summary["disk_pct"] = round(usage.percent, 2)

    keep_min = max(0, _int_env(ARCHIVE_KEEP_MIN_ENV, DEFAULT_ARCHIVE_KEEP_MIN))
    max_bytes = _int_env(ARCHIVE_MAX_BYTES_ENV, DEFAULT_ARCHIVE_MAX_BYTES)
    retention_days = _int_env(ARCHIVE_RETENTION_DAYS_ENV, DEFAULT_ARCHIVE_RETENTION_DAYS)
    watermark_pct = _float_env(DISK_HIGH_WATERMARK_PCT_ENV, DEFAULT_DISK_HIGH_WATERMARK_PCT)

    # The newest keep_min are pinned and never even considered for deletion.
    if len(entries) <= keep_min:
        summary["skipped"] = "at_or_below_keep_min"
        _emit(summary)
        return summary
    deletable = entries[: len(entries) - keep_min]  # oldest-first slice

    over_watermark = (
        usage is not None and watermark_pct > 0 and usage.percent >= watermark_pct
    )
    # The size-drain target. Normally the size cap. When the DATA_DIR volume is
    # at/over the high-watermark we prune AGGRESSIVELY: drain to half the cap so
    # the janitor reclaims real headroom instead of hovering at the cap while the
    # disk is still critically full. Never overrides keep_min or age safety.
    effective_target = max_bytes
    if over_watermark and max_bytes > 0:
        effective_target = max_bytes // 2

    age_cutoff_epoch: float | None = None
    if retention_days > 0:
        age_cutoff_epoch = time.time() - retention_days * 86400.0

    running_total = before_bytes
    removed = 0
    freed = 0

    for item in deletable:
        too_old = age_cutoff_epoch is not None and item.age_key < age_cutoff_epoch
        # Size pressure: a size cap is set and we are still over the effective
        # drain target (the cap, or half the cap under high-watermark pressure).
        # Once back under the target, only the age cap can keep deleting.
        size_pressure = effective_target > 0 and running_total > effective_target

        should_delete = too_old or size_pressure
        if not should_delete:
            # Entries are strictly oldest-first: if this (oldest survivor) is
            # neither too old nor needed to relieve size pressure, no younger
            # entry can be either. Stop.
            break

        freed_json = _safe_unlink(item.json_path, archive_dir)
        freed_source = 0
        if item.source_path is not None:
            freed_source = _safe_unlink(item.source_path, archive_dir)
        entry_freed = freed_json + freed_source
        if freed_json == 0 and freed_source == 0:
            # Guard refused this entry (vanished / escape). Skip WITHOUT
            # decrementing the running total so we keep draining real bytes.
            continue
        removed += 1
        freed += entry_freed
        running_total -= item.size_bytes

    summary["removed"] = removed
    summary["freed_bytes"] = freed
    summary["after_bytes"] = max(0, before_bytes - freed)
    if freed:
        telemetry.increment("archive_janitor_matters_removed", removed)
        telemetry.increment("archive_janitor_bytes_freed", freed)
    _emit(summary)
    return summary


def maybe_run_archive_rotation(*, force: bool = False) -> dict[str, Any] | None:
    """Rate-limited entry point for the background poll loop.

    Runs ``run_archive_rotation`` at most once per
    ``NDA_ARCHIVE_ROTATION_MIN_INTERVAL_SECONDS`` (default hourly) unless
    ``force`` (used for the once-at-startup call). Never raises -- a janitor
    error must never break the poll.
    """
    try:
        interval = _int_env(
            ARCHIVE_ROTATION_MIN_INTERVAL_ENV,
            DEFAULT_ARCHIVE_ROTATION_MIN_INTERVAL_SECONDS,
        )
        if not force and interval > 0:
            elapsed = time.monotonic() - _LAST_ROTATION_MONOTONIC
            if _LAST_ROTATION_MONOTONIC > 0 and elapsed < interval:
                return None
        return run_archive_rotation()
    except Exception:  # pragma: no cover - defensive: janitor never breaks caller.
        return None
