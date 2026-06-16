"""Stdlib-only process-memory probes for prod OOM observability.

The 2026-06-16 OOM firefight taught one lesson: *measure, don't guess*. These
helpers expose how close the worker is running to its container memory limit so
the headroom is observable on the deployment-status endpoint instead of inferred
after a crash-loop.

Hard constraints (mirroring the guarded ``import resource`` at
``document_rendering.py:20``):

* STDLIB ONLY -- no ``psutil``. Resident-set size comes from ``/proc/self/statm``
  (Linux) with a ``resource.getrusage`` fallback; the container limit comes from
  the cgroup v2/v1 pseudo-files.
* EVERY probe is fail-safe: any read/parse error returns ``None`` (or a no-op),
  never raises. A probe must never crash boot or a request handler.
* DEGRADE TO "unknown", never to a false red. On macOS / local dev the cgroup
  files are absent and ``container_memory_limit_bytes()`` returns ``None`` -- the
  caller treats an unknown limit as advisory, never as a breach.
"""

from __future__ import annotations

import os

try:  # POSIX-only; absent on Windows. Mirrors document_rendering.py:20.
    import resource
except ImportError:  # pragma: no cover - non-POSIX fallback
    resource = None  # type: ignore[assignment]

# cgroup pseudo-files that publish the container memory ceiling. v2 (unified
# hierarchy) is checked first since modern hosts (incl. Render) use it; v1 is the
# legacy fallback. Both can hold a sentinel ("max"/a huge number) meaning "no
# limit", which we treat as unknown so we never compute a bogus headroom.
_CGROUP_V2_MEMORY_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V1_MEMORY_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"

# A v1 "unlimited" limit is a near-word-size sentinel (e.g. 0x7FFF_FFFF_FFFF_F000).
# Anything at or above this is treated as "no limit" -> unknown. 2**62 is a safe
# floor: comfortably above any real container cap, comfortably below the sentinel.
_NO_LIMIT_FLOOR = 1 << 62

# Module-global cache for the container limit: it is fixed for the life of the
# process (the cgroup ceiling does not change under us), so we read the
# pseudo-files at most once. ``_LIMIT_CACHED`` distinguishes "cached None
# (unreadable)" from "not yet probed".
_cached_limit_bytes: int | None = None
_limit_cached: bool = False


def current_rss_bytes() -> int | None:
    """Current resident-set size of this process in BYTES, or ``None`` on failure.

    Primary source: ``/proc/self/statm`` field 2 (resident pages) x the system
    page size -- cheap, exact, and Linux-native (where the worker actually runs).
    Fallback: ``resource.getrusage(RUSAGE_SELF).ru_maxrss``, whose unit differs by
    platform -- BYTES on macOS/BSD, KiB on Linux -- so we branch on
    ``sys.platform``. Note the fallback is a *peak* (high-water mark), not the live
    RSS, so it is strictly a degraded best-effort when ``/proc`` is unavailable.

    Returns ``None`` (never raises) if neither source can be read/parsed.
    """
    rss = _rss_from_proc_statm()
    if rss is not None:
        return rss
    return _rss_from_getrusage()


def _rss_from_proc_statm() -> int | None:
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            fields = handle.read().split()
        # statm: size resident shared text lib data dt (all in pages).
        resident_pages = int(fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        if resident_pages < 0 or page_size <= 0:
            return None
        return resident_pages * page_size
    except (OSError, ValueError, IndexError, AttributeError):
        # No /proc (macOS), malformed contents, or os.sysconf unsupported.
        return None


def _rss_from_getrusage() -> int | None:
    if resource is None:
        return None
    try:
        ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):  # pragma: no cover - getrusage rarely fails
        return None
    if not isinstance(ru_maxrss, int) or ru_maxrss <= 0:
        return None
    import sys

    if sys.platform == "darwin":
        # macOS / BSD report ru_maxrss in BYTES already.
        return ru_maxrss
    # Linux (and most other Unixes) report ru_maxrss in KiB.
    return ru_maxrss * 1024


def container_memory_limit_bytes() -> int | None:
    """Container memory ceiling in BYTES, or ``None`` when there is no real limit.

    Reads the cgroup v2 ``memory.max`` then the v1 ``memory.limit_in_bytes``
    pseudo-file. A "max"/sentinel value (no cgroup limit, e.g. on a bare host or
    macOS where the files are absent entirely) yields ``None`` -- the caller treats
    an unknown limit as advisory so headroom is reported but never flagged red.

    The result is cached module-globally on first call: the cgroup ceiling is fixed
    for the life of the process. NEVER raises.
    """
    global _cached_limit_bytes, _limit_cached
    if _limit_cached:
        return _cached_limit_bytes
    limit = _read_cgroup_v2_limit()
    if limit is None:
        limit = _read_cgroup_v1_limit()
    _cached_limit_bytes = limit
    _limit_cached = True
    return limit


def _read_cgroup_v2_limit() -> int | None:
    raw = _read_pseudo_file(_CGROUP_V2_MEMORY_MAX)
    if raw is None:
        return None
    if raw == "max":
        # v2 spells "no limit" literally.
        return None
    return _coerce_limit(raw)


def _read_cgroup_v1_limit() -> int | None:
    raw = _read_pseudo_file(_CGROUP_V1_MEMORY_LIMIT)
    if raw is None:
        return None
    return _coerce_limit(raw)


def _read_pseudo_file(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read().strip()
    except (OSError, ValueError):
        return None


def _coerce_limit(raw: str) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value >= _NO_LIMIT_FLOOR:
        # Non-positive, or the "unlimited" sentinel -> treat as no real limit.
        return None
    return value


def _reset_limit_cache_for_tests() -> None:
    """Drop the cached container limit so a test can re-probe with new fakes."""
    global _cached_limit_bytes, _limit_cached
    _cached_limit_bytes = None
    _limit_cached = False


def memory_usage() -> dict[str, int | float | None]:
    """Assemble the additive ``memory`` block for the deployment-status payload.

    Returns ``rss_bytes`` / ``limit_bytes`` / ``headroom_bytes`` / ``used_fraction``,
    each ``None`` when it cannot be derived (RSS unreadable, or no container limit).
    Pure-ish wrapper over the two probes; never raises.
    """
    rss = current_rss_bytes()
    limit = container_memory_limit_bytes()
    headroom: int | None = None
    used_fraction: float | None = None
    if rss is not None and limit is not None and limit > 0:
        headroom = limit - rss
        used_fraction = rss / limit
    return {
        "rss_bytes": rss,
        "limit_bytes": limit,
        "headroom_bytes": headroom,
        "used_fraction": used_fraction,
    }
