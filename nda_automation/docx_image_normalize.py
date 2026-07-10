"""Normalize EMF/WMF vector images inside a DOCX into browser-renderable PNG.

Why this exists
---------------
Office pastes vector logos as EMF/WMF parts. docx-preview (the in-browser DOCX
renderer) emits an ``<img>`` with a generic-mime data URL for those parts, and no
browser ships an EMF/WMF decoder, so the logo renders as a blank 0x0 box. PNG and
JPEG logos render fine. This helper rewrites a DOCX so every EMF/WMF media part
becomes a PNG, keeping the relationship ids stable (the ``<a:blip r:embed=...>``
references resolve unchanged), so the logo shows up in the body and header/footer.

Contract
--------
``normalize_docx_emf_wmf_images(docx_bytes) -> bytes`` is a PURE function: DOCX in,
DOCX out. It:
  * finds ``word/media/*.emf`` / ``*.wmf`` parts,
  * converts each to PNG via LibreOffice/soffice or ImageMagick when available,
    substituting a small placeholder PNG when no conversion tool is present or a
    given image fails to convert (so the result is NEVER a blank box),
  * renames the part to ``.png`` and rewrites the referencing ``*.rels`` Targets,
  * ensures ``[Content_Types].xml`` declares the ``png`` default extension and
    drops now-unused ``emf``/``wmf`` declarations,
  * leaves a DOCX with no EMF/WMF media parts unchanged (returns the input bytes).

Integration note (NOT wired here)
---------------------------------
This helper is intentionally not called from any endpoint handler in this lane.
The backend-endpoints lane should call it on the DOCX bytes just before they are
handed to the browser DOCX preview path -- i.e. in the source-docx / reviewed-docx
serving handlers (server.do_GET -> matter_routes.handle_matter_source_docx and
approval_routes.handle_matter_reviewed_docx), after the bytes are loaded and
before they are sent. It is cheap and a no-op for DOCX without EMF/WMF media.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from io import BytesIO
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from .docx_xml import _default_namespace_registration

LOGGER = logging.getLogger(__name__)

CONTENT_TYPES_PART = "[Content_Types].xml"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

EMF_CONTENT_TYPE = "image/x-emf"
WMF_CONTENT_TYPE = "image/x-wmf"
PNG_CONTENT_TYPE = "image/png"

# Extensions we normalize, mapped to their declared content types.
VECTOR_IMAGE_EXTENSIONS = {"emf": EMF_CONTENT_TYPE, "wmf": WMF_CONTENT_TYPE}

DEFAULT_CONVERSION_TIMEOUT_SECONDS = 30

# Per-document conversion bounds. Each EMF/WMF -> PNG conversion spawns a
# heavyweight soffice child, so a document with hundreds of embedded vector
# images could otherwise hold a request thread for many minutes and DoS the
# 1-CPU/2GB box. We cap how many images we will CONVERT per document (cache hits
# are free and never counted) and the total wall-clock spent converting; past
# either bound the remaining vector parts are left un-normalized (they render as
# a blank box -- the degraded-but-safe fallback) and a warning is logged.
MAX_IMAGES_CONVERTED_PER_DOCUMENT = 20
CONVERSION_WALL_CLOCK_BUDGET_SECONDS = 60.0

# On-disk conversion cache. The same letterhead logo recurs on essentially every
# reviewed-docx / source-docx request and across process restarts, so conversions
# are memoized by content hash. Bounded by entry count with LRU-by-mtime eviction.
IMAGE_CONVERSION_CACHE_DIRNAME = "image-normalize"
MAX_IMAGE_CONVERSION_CACHE_ENTRIES = 512
MAX_IMAGE_CONVERSION_MEMORY_ENTRIES = 256

# Sentinel so callers can distinguish "use the shared production cache" (default)
# from "explicitly disable caching" (image_cache=None) and "use this cache".
_UNSET_CACHE: object = object()

# A VALID 1x1 transparent RGBA PNG. Used as the guaranteed fallback when no
# EMF/WMF conversion tool is available (or a specific image fails to convert), so
# the rewritten DOCX never carries an undecodable vector part that renders blank.
# Every byte here matters: this must DECODE in a real renderer (PIL/browser), not
# merely be non-empty. Its IDAT is a genuine zlib stream of one filtered
# transparent pixel, with correct chunk lengths and CRCs (verified by the unit
# test, which asserts zlib round-trips the IDAT and a decoder opens the image).
PLACEHOLDER_PNG_BYTES = bytes(
    [
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG signature
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,  # IHDR length(13) + type
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,  # width=1, height=1
        0x08, 0x06, 0x00, 0x00, 0x00,                    # depth=8, colour=6(RGBA), ...
        0x1F, 0x15, 0xC4, 0x89,                          # IHDR CRC
        0x00, 0x00, 0x00, 0x0B, 0x49, 0x44, 0x41, 0x54,  # IDAT length(11) + type
        0x78, 0xDA, 0x63, 0x60, 0x00, 0x02, 0x00,        # zlib: deflate of one transparent pixel
        0x00, 0x05, 0x00, 0x01,                          # zlib adler32 tail
        0xE9, 0xFA, 0xDC, 0xD8,                          # IDAT CRC
        0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44,  # IEND length(0) + type
        0xAE, 0x42, 0x60, 0x82,                          # IEND CRC
    ]
)


@dataclass(frozen=True)
class _MediaPart:
    name: str  # full archive path, e.g. "word/media/image1.emf"
    extension: str  # lowercase, e.g. "emf"


# ImageConverter: bytes (EMF/WMF) -> PNG bytes, or None when it cannot convert.
ImageConverter = Callable[[bytes, str], bytes | None]


def normalize_docx_emf_wmf_images(
    docx_bytes: bytes,
    *,
    converter: ImageConverter | None = None,
    image_cache: "_ImageConversionCache | None | object" = _UNSET_CACHE,
    max_images: int = MAX_IMAGES_CONVERTED_PER_DOCUMENT,
    time_budget_seconds: float = CONVERSION_WALL_CLOCK_BUDGET_SECONDS,
) -> bytes:
    """Return DOCX bytes with EMF/WMF media parts converted to PNG.

    Pure: does not mutate the input. A DOCX with no EMF/WMF media is returned
    unchanged (the original bytes). ``converter`` is injectable for testing; the
    default tries LibreOffice/soffice then ImageMagick, then a placeholder PNG.

    Conversion is bounded three ways so a hostile or pathological document can
    never turn a single serve request into a soffice storm:

    * ``image_cache`` memoizes conversions by content hash (in-process + bounded
      disk). Defaulted to the shared production cache for the default converter,
      so the same letterhead logo is converted once per process, not per request.
      Pass ``None`` to disable caching, or a specific cache to control it (tests).
    * ``max_images`` caps how many images we CONVERT per document (cache hits are
      free and do not count).
    * ``time_budget_seconds`` caps the total wall-clock spent converting.

    Once a bound is hit the remaining vector parts are left un-normalized and a
    warning is logged -- a document with 200 EMFs must never hold a request thread
    for 100 minutes. Conversion failures fail soft to a placeholder PNG.
    """
    try:
        with ZipFile(BytesIO(docx_bytes)) as archive:
            names = archive.namelist()
            vector_parts = _vector_media_parts(names)
            if not vector_parts:
                return docx_bytes
            members = {name: archive.read(name) for name in names}
    except Exception:
        # Not a readable zip / DOCX: hand the bytes back untouched rather than
        # raise. Callers serve documents; a normalization pass must never be the
        # thing that breaks serving one.
        return docx_bytes

    active_converter = converter or _default_image_converter()
    if image_cache is _UNSET_CACHE:
        # Default: production serve paths pass no converter and get the shared
        # disk-backed cache. A test injecting its own converter (but no explicit
        # cache) keeps the historical uncached behavior so call-count assertions
        # stay meaningful.
        cache = _shared_image_cache() if converter is None else None
    else:
        cache = image_cache  # explicit: a specific cache, or None to disable

    rename_map: dict[str, str] = {}  # old archive path -> new archive path
    new_members: dict[str, bytes] = dict(members)

    started = time.monotonic()
    conversions_done = 0
    skipped = 0

    for part in vector_parts:
        data = members[part.name]

        png_bytes = cache.lookup(data) if cache is not None else None
        if png_bytes is None:
            budget_left = conversions_done < max_images and (
                time.monotonic() - started
            ) < time_budget_seconds
            if not budget_left:
                # Cap/budget exhausted: leave this vector part untouched. It will
                # render as a blank box, but the request thread is protected.
                skipped += 1
                continue
            if cache is not None:
                png_bytes, converted = cache.convert_and_store(
                    data, part.extension, active_converter
                )
                if converted:
                    conversions_done += 1
            else:
                png_bytes = _convert_or_placeholder(active_converter, data, part.extension)
                conversions_done += 1

        new_name = _png_part_name(part.name)
        new_members.pop(part.name, None)
        new_members[new_name] = png_bytes
        rename_map[part.name] = new_name

    if skipped:
        LOGGER.warning(
            "EMF/WMF normalization capped: converted %d image(s), skipped %d of %d "
            "vector part(s) after hitting the per-document cap (%d) / wall-clock "
            "budget (%.0fs); skipped parts left un-normalized.",
            conversions_done,
            skipped,
            len(vector_parts),
            max_images,
            time_budget_seconds,
        )

    new_members = _rewrite_relationships(new_members, rename_map)
    new_members[CONTENT_TYPES_PART] = _rewrite_content_types(
        new_members.get(CONTENT_TYPES_PART, b""), rename_map
    )

    return _repackage(new_members, original_order=names, rename_map=rename_map)


def _vector_media_parts(names: list[str]) -> list[_MediaPart]:
    parts: list[_MediaPart] = []
    for name in names:
        lowered = name.lower()
        if not lowered.startswith("word/media/"):
            continue
        extension = lowered.rsplit(".", 1)[-1] if "." in lowered else ""
        if extension in VECTOR_IMAGE_EXTENSIONS:
            parts.append(_MediaPart(name=name, extension=extension))
    return parts


def _png_part_name(name: str) -> str:
    head = name.rsplit(".", 1)[0] if "." in name else name
    return f"{head}.png"


def _convert_or_placeholder(converter: ImageConverter, data: bytes, extension: str) -> bytes:
    try:
        converted = converter(data, extension)
    except Exception:
        converted = None
    if converted and converted.startswith(b"\x89PNG\r\n\x1a\n"):
        return converted
    return PLACEHOLDER_PNG_BYTES


# --------------------------------------------------------------------------- #
# Content-hash conversion cache (in-process LRU + bounded disk)
# --------------------------------------------------------------------------- #


class _ImageConversionCache:
    """Memoize EMF/WMF -> PNG conversions keyed on ``sha256(image bytes)``.

    Two tiers because the same letterhead logo recurs on nearly every serve
    request AND across process restarts:

    * an in-process LRU (``OrderedDict``) for hot reuse within a worker, and
    * a bounded on-disk store (``<dir>/<sha256>.png``) so a freshly started
      worker does not reconvert what a previous one already produced.

    The document-render cache (``document_rendering._enforce_render_cache_bound``)
    is keyed on ``(document bytes + owner)`` with per-entry metadata directories
    and is not cleanly reusable for a flat bytes->bytes image store, so this is a
    small purpose-built cache that MIRRORS its eviction policy (bounded entry
    count, LRU by file mtime). Keying purely on content is correct and safe to
    share across tenants: the PNG is a deterministic function of the input image
    bytes and carries no tenant data.

    Every disk operation is best-effort: a cache error must never fail (or slow to
    a crawl) the serve request that triggered it, so failures degrade to a miss.
    Conversions for the SAME content are deduplicated under a per-key lock, so two
    concurrent first-time requests for one logo convert once, not twice.
    """

    def __init__(self, cache_dir: Path | None = None, *, max_entries: int = MAX_IMAGE_CONVERSION_CACHE_ENTRIES) -> None:
        self._explicit_dir = cache_dir
        self._max_entries = max_entries
        self._memory: "OrderedDict[str, bytes]" = OrderedDict()
        self._memory_lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    @staticmethod
    def _key(data: bytes) -> str:
        # Target format is always PNG here, so the image bytes fully determine the
        # output; no need to fold a format token into the key.
        return hashlib.sha256(data).hexdigest()

    def _cache_dir(self) -> Path | None:
        if self._explicit_dir is not None:
            return self._explicit_dir
        try:
            from . import matter_store  # lazy: avoid import cost when unused

            return matter_store.DATA_DIR / "cache" / IMAGE_CONVERSION_CACHE_DIRNAME
        except Exception:
            return None

    def lookup(self, data: bytes) -> bytes | None:
        """Return cached PNG bytes for ``data`` from memory or disk, or None."""
        key = self._key(data)
        with self._memory_lock:
            hit = self._memory.get(key)
            if hit is not None:
                self._memory.move_to_end(key)
                return hit
        disk = self._disk_get(key)
        if disk is not None:
            self._memory_put(key, disk)
        return disk

    def convert_and_store(
        self, data: bytes, extension: str, converter: ImageConverter
    ) -> tuple[bytes, bool]:
        """Convert ``data`` (once per content, under a per-key lock) and cache the
        resolved PNG. Returns ``(png_bytes, converted)`` where ``converted`` is
        True only if this call actually invoked the converter (so the caller can
        count it against the per-document cap)."""
        key = self._key(data)
        with self._key_lock(key):
            hit = self._memory_get(key)
            if hit is None:
                hit = self._disk_get(key)
                if hit is not None:
                    self._memory_put(key, hit)
            if hit is not None:
                return hit, False
            resolved = _convert_or_placeholder(converter, data, extension)
            self._memory_put(key, resolved)
            self._disk_put(key, resolved)
            return resolved, True

    def _key_lock(self, key: str) -> threading.Lock:
        with self._key_locks_guard:
            # Opportunistically bound the lock table; dropping a lock only risks a
            # rare benign double-convert, never corruption.
            if len(self._key_locks) > 4 * self._max_entries:
                self._key_locks.clear()
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _memory_get(self, key: str) -> bytes | None:
        with self._memory_lock:
            hit = self._memory.get(key)
            if hit is not None:
                self._memory.move_to_end(key)
            return hit

    def _memory_put(self, key: str, value: bytes) -> None:
        with self._memory_lock:
            self._memory[key] = value
            self._memory.move_to_end(key)
            while len(self._memory) > MAX_IMAGE_CONVERSION_MEMORY_ENTRIES:
                self._memory.popitem(last=False)

    def _disk_get(self, key: str) -> bytes | None:
        cache_dir = self._cache_dir()
        if cache_dir is None:
            return None
        path = cache_dir / f"{key}.png"
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:  # bump mtime so LRU eviction spares a hot entry
            os.utime(path, None)
        except OSError:
            pass
        return data

    def _disk_put(self, key: str, value: bytes) -> None:
        cache_dir = self._cache_dir()
        if cache_dir is None:
            return
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            path = cache_dir / f"{key}.png"
            # Atomic publish: write a temp file then rename so a concurrent reader
            # never sees a half-written PNG.
            fd, tmp_name = tempfile.mkstemp(dir=str(cache_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(value)
                os.replace(tmp_name, path)
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                return
        except OSError:
            return
        self._enforce_disk_bound(cache_dir, keep=path)

    def _enforce_disk_bound(self, cache_dir: Path, *, keep: Path) -> None:
        """Evict least-recently-used cache files (by mtime) beyond the bound.

        Mirrors ``document_rendering._enforce_render_cache_bound`` for a flat file
        cache. Best-effort: a prune failure must never fail the conversion."""
        try:
            entries = [child for child in cache_dir.iterdir() if child.suffix == ".png" and child.is_file()]
        except OSError:
            return
        if len(entries) <= self._max_entries:
            return
        keep_resolved = keep.resolve()

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        for entry in sorted(entries, key=_mtime):
            if len(entries) <= self._max_entries:
                break
            if entry.resolve() == keep_resolved:
                continue
            try:
                entry.unlink()
            except OSError:
                pass
            entries.remove(entry)


_SHARED_IMAGE_CACHE: _ImageConversionCache | None = None
_SHARED_IMAGE_CACHE_LOCK = threading.Lock()


def _shared_image_cache() -> _ImageConversionCache:
    """Process-wide singleton cache used by the default (production) serve paths."""
    global _SHARED_IMAGE_CACHE
    if _SHARED_IMAGE_CACHE is None:
        with _SHARED_IMAGE_CACHE_LOCK:
            if _SHARED_IMAGE_CACHE is None:
                _SHARED_IMAGE_CACHE = _ImageConversionCache()
    return _SHARED_IMAGE_CACHE


# --------------------------------------------------------------------------- #
# Relationship + content-type rewriting
# --------------------------------------------------------------------------- #


def _rewrite_relationships(members: dict[str, bytes], rename_map: dict[str, str]) -> dict[str, bytes]:
    """Rewrite ``*.rels`` Targets that point at a renamed media part.

    Relationship Targets are written relative to the rels file's owning part
    directory (e.g. ``media/image1.emf`` from ``word/_rels/document.xml.rels``),
    so we compare the resolved absolute part path, not the raw Target string.
    """
    if not rename_map:
        return members
    updated = dict(members)
    for name, data in members.items():
        if not name.endswith(".rels"):
            continue
        rewritten = _rewrite_rels_part(name, data, rename_map)
        if rewritten is not None:
            updated[name] = rewritten
    return updated


def _rewrite_rels_part(rels_name: str, data: bytes, rename_map: dict[str, str]) -> bytes | None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    base_dir = _rels_base_dir(rels_name)
    changed = False
    rel_tag = f"{{{RELATIONSHIPS_NS}}}Relationship"
    for relationship in root.iter(rel_tag):
        target = relationship.get("Target")
        mode = (relationship.get("TargetMode") or "").lower()
        if not target or mode == "external":
            continue
        resolved = _resolve_relative_part(base_dir, target)
        if resolved in rename_map:
            new_target = _relative_target(base_dir, rename_map[resolved])
            relationship.set("Target", new_target)
            changed = True
    if not changed:
        return None
    return _serialize_xml(root, default_ns=RELATIONSHIPS_NS)


def _rels_base_dir(rels_name: str) -> str:
    # "word/_rels/document.xml.rels" -> "word"; "_rels/.rels" -> "".
    directory = os.path.dirname(rels_name)
    if directory.endswith("_rels"):
        directory = os.path.dirname(directory)
    return directory


def _resolve_relative_part(base_dir: str, target: str) -> str:
    target = target.lstrip("/")
    if target.startswith("../") or base_dir == "":
        joined = os.path.normpath(os.path.join(base_dir, target)) if base_dir else os.path.normpath(target)
    else:
        joined = os.path.normpath(os.path.join(base_dir, target))
    return joined.replace(os.sep, "/").lstrip("./")


def _relative_target(base_dir: str, part_name: str) -> str:
    if not base_dir:
        return part_name
    relative = os.path.relpath(part_name, base_dir)
    return relative.replace(os.sep, "/")


def _rewrite_content_types(data: bytes, rename_map: dict[str, str]) -> bytes:
    """Ensure png is declared and prune now-unused emf/wmf declarations.

    Adds a ``<Default Extension="png" ContentType="image/png"/>`` if absent,
    rewrites any ``<Override>`` that named a renamed part, and removes
    ``<Default Extension="emf|wmf">`` declarations that no remaining part uses.
    """
    try:
        root = ET.fromstring(data) if data else ET.Element(f"{{{CONTENT_TYPES_NS}}}Types")
    except ET.ParseError:
        return data

    default_tag = f"{{{CONTENT_TYPES_NS}}}Default"
    override_tag = f"{{{CONTENT_TYPES_NS}}}Override"

    have_png_default = any(
        child.tag == default_tag and (child.get("Extension") or "").lower() == "png"
        for child in root
    )

    # Rewrite Overrides that named a renamed EMF/WMF part.
    for child in list(root):
        if child.tag != override_tag:
            continue
        part_name = (child.get("PartName") or "").lstrip("/")
        if part_name in rename_map:
            child.set("PartName", "/" + rename_map[part_name])
            child.set("ContentType", PNG_CONTENT_TYPE)

    # Drop Default declarations for emf/wmf (the parts no longer exist).
    for child in list(root):
        if child.tag == default_tag and (child.get("Extension") or "").lower() in VECTOR_IMAGE_EXTENSIONS:
            root.remove(child)

    if not have_png_default:
        default = ET.SubElement(root, default_tag)
        default.set("Extension", "png")
        default.set("ContentType", PNG_CONTENT_TYPE)

    return _serialize_xml(root, default_ns=CONTENT_TYPES_NS)


def _serialize_xml(root: ET.Element, *, default_ns: str) -> bytes:
    # Register the element's namespace as the default so the serialized package
    # part keeps the expected unprefixed shape OPC consumers expect.
    #
    # ``ET.register_namespace`` mutates the PROCESS-GLOBAL ``ET._namespace_map``.
    # Doing it unscoped would (a) permanently change the prefix every later
    # ElementTree serialization anywhere in the process emits, and (b) race with
    # any other thread registering the empty prefix for a DIFFERENT uri (the app
    # renders documents 2-concurrently), cross-contaminating output. Route it
    # through the shared, lock-held snapshot/restore context manager so the map is
    # always returned to its prior contents -- even on exception -- and concurrent
    # serializations of different default namespaces cannot interleave.
    with _default_namespace_registration(default_ns):
        body = ET.tostring(root, encoding="utf-8")
    if not body.lstrip().startswith(b"<?xml"):
        body = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n' + body
    return body


def _repackage(
    members: dict[str, bytes],
    *,
    original_order: list[str],
    rename_map: dict[str, str],
) -> bytes:
    buffer = BytesIO()
    written: set[str] = set()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        # Preserve original ordering where possible (OPC tolerates any order, but
        # keeping [Content_Types].xml-first ordering stable avoids surprises).
        for name in original_order:
            target_name = rename_map.get(name, name)
            if target_name in members and target_name not in written:
                archive.writestr(target_name, members[target_name])
                written.add(target_name)
        for name, data in members.items():
            if name not in written:
                archive.writestr(name, data)
                written.add(name)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Default converters (soffice / ImageMagick), with a placeholder fallback
# --------------------------------------------------------------------------- #


def _default_image_converter() -> ImageConverter:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    magick = shutil.which("magick") or shutil.which("convert")

    def _convert(data: bytes, extension: str) -> bytes | None:
        if soffice:
            png = _convert_with_soffice(soffice, data, extension)
            if png is not None:
                return png
        if magick:
            png = _convert_with_imagemagick(magick, data, extension)
            if png is not None:
                return png
        return None

    return _convert


def _convert_with_soffice(executable: str, data: bytes, extension: str) -> bytes | None:
    # Route through the SAME process-wide soffice controls the DOCX->PDF path
    # uses: the shared BoundedSemaphore (do NOT spin up a second one -- that would
    # double real concurrency on the 1-CPU box), plus the RLIMIT_AS/RLIMIT_CPU
    # child limits and process-group kill baked into ``_run_soffice_command``.
    # Lazily imported so this module stays importable (and cheap) without pulling
    # in the rendering stack, and to avoid any import cycle.
    from . import document_rendering as _dr

    with tempfile.TemporaryDirectory(prefix="nda-emf-") as tmp_name:
        tmp_dir = Path(tmp_name)
        source = tmp_dir / f"image.{extension}"
        source.write_bytes(data)
        command = [
            executable,
            "--headless",
            "--convert-to",
            "png",
            "--outdir",
            str(tmp_dir),
            str(source),
        ]
        # Backpressure: if no slot frees within the queue wait, fall through to a
        # placeholder rather than block a request thread (fail-soft).
        if not _dr._SOFFICE_CONVERSION_SEMAPHORE.acquire(timeout=_dr.CONVERSION_QUEUE_WAIT_SECONDS):
            return None
        try:
            returncode, _stdout, _stderr = _dr._run_soffice_command(
                command,
                cwd=str(tmp_dir),
                timeout_seconds=DEFAULT_CONVERSION_TIMEOUT_SECONDS,
            )
            if returncode != 0:
                return None
            output = tmp_dir / "image.png"
            if output.is_file():
                return output.read_bytes()
            return None
        except Exception:
            # Timeout / conversion error / missing executable / OSError: every
            # failure fails soft to the placeholder; normalization never raises.
            return None
        finally:
            _dr._SOFFICE_CONVERSION_SEMAPHORE.release()


def _convert_with_imagemagick(executable: str, data: bytes, extension: str) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="nda-emf-") as tmp_name:
        tmp_dir = Path(tmp_name)
        source = tmp_dir / f"image.{extension}"
        output = tmp_dir / "image.png"
        source.write_bytes(data)
        command = [executable, str(source), str(output)]
        try:
            subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=DEFAULT_CONVERSION_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if output.is_file():
            try:
                return output.read_bytes()
            except OSError:
                return None
        return None
