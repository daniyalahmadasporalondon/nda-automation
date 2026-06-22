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

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

CONTENT_TYPES_PART = "[Content_Types].xml"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

EMF_CONTENT_TYPE = "image/x-emf"
WMF_CONTENT_TYPE = "image/x-wmf"
PNG_CONTENT_TYPE = "image/png"

# Extensions we normalize, mapped to their declared content types.
VECTOR_IMAGE_EXTENSIONS = {"emf": EMF_CONTENT_TYPE, "wmf": WMF_CONTENT_TYPE}

DEFAULT_CONVERSION_TIMEOUT_SECONDS = 30

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
) -> bytes:
    """Return DOCX bytes with EMF/WMF media parts converted to PNG.

    Pure: does not mutate the input. A DOCX with no EMF/WMF media is returned
    unchanged (the original bytes). ``converter`` is injectable for testing; the
    default tries LibreOffice/soffice then ImageMagick, then a placeholder PNG.
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

    rename_map: dict[str, str] = {}  # old archive path -> new archive path
    new_members: dict[str, bytes] = dict(members)

    for part in vector_parts:
        png_bytes = _convert_or_placeholder(active_converter, members[part.name], part.extension)
        new_name = _png_part_name(part.name)
        new_members.pop(part.name, None)
        new_members[new_name] = png_bytes
        rename_map[part.name] = new_name

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
    ET.register_namespace("", default_ns)
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
        try:
            subprocess.run(
                command,
                cwd=str(tmp_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=DEFAULT_CONVERSION_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        output = tmp_dir / "image.png"
        if output.is_file():
            try:
                return output.read_bytes()
            except OSError:
                return None
        return None


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
