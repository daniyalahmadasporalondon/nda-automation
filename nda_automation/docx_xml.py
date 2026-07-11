from __future__ import annotations

import codecs
import contextlib
import re
import threading
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Dict, Iterator

UNSAFE_XML_DECLARATION_MESSAGE = "The Word document contains unsupported XML DTD/entity declarations."
_UNSAFE_XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_ENCODING_DECLARATION = re.compile(
    r"^\ufeff?\s*<\?xml\s+[^>]*\bencoding\s*=\s*(['\"])(?P<encoding>[^'\"]+)\1",
    re.IGNORECASE,
)


# Unicode presentation-form ligatures a PDF/DOCX text layer routinely carries in
# place of their component letters ("Con<ﬁ>dential", "con<ﬂ>ict"). They are pure
# GLYPH artefacts -- the semantic text is "fi"/"fl"/etc -- so a deterministic clause
# regex written in plain ASCII ("confidential") never matches a paragraph that
# stores the ligature codepoint, and the working-DOCX conversion's normalized text
# comparison can diverge when one engine folds the ligature and the other does not.
# Folding them to their ASCII components at every normalization/matching seam keeps
# extraction, matching and the PDF->DOCX anchor mapping consistent. This is the
# single source of truth imported by ``checks.common`` and ``pdf_ingest_conversion``.
LIGATURE_TRANSLATION = {
    "ﬀ": "ff",   # ﬀ
    "ﬁ": "fi",   # ﬁ
    "ﬂ": "fl",   # ﬂ
    "ﬃ": "ffi",  # ﬃ
    "ﬄ": "ffl",  # ﬄ
    "ﬅ": "st",   # ﬅ (long-s t)
    "ﬆ": "st",   # ﬆ
}
_LIGATURE_TABLE = {ord(key): value for key, value in LIGATURE_TRANSLATION.items()}


def fold_ligatures(text: str) -> str:
    """Replace Unicode presentation-form ligatures with their ASCII components.

    ``fold_ligatures("Conﬁdential")`` -> ``"Confidential"``. Idempotent and a
    no-op (same object semantics) for text with no ligature codepoint, so a normal
    ASCII document is unchanged. Folds ﬀ/ﬁ/ﬂ/ﬃ/ﬄ (and the archaic ﬅ/ﬆ st-ligatures)."""
    if not text:
        return text
    return text.translate(_LIGATURE_TABLE)


class UnsafeDocxXmlError(ValueError):
    """Raised when an OOXML part contains DTD/entity declarations."""


def is_docx_xml_part(part_name: str) -> bool:
    return part_name.endswith(".xml") or part_name.endswith(".rels")


def _sniff_xml_encoding(data: bytes) -> str | None:
    if data.startswith((codecs.BOM_UTF32_BE, codecs.BOM_UTF32_LE)):
        return "utf-32"
    if data.startswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
        return "utf-16"
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if data.startswith(b"\x00\x00\x00<"):
        return "utf-32-be"
    if data.startswith(b"<\x00\x00\x00"):
        return "utf-32-le"
    if data.startswith(b"\x00<"):
        return "utf-16-be"
    if data.startswith(b"<\x00"):
        return "utf-16-le"
    return _sniff_null_padded_xml_encoding(data)


def _sniff_null_padded_xml_encoding(data: bytes) -> str | None:
    sample = data[:256]
    if len(sample) < 8:
        return None

    slots = [sample[index::4] for index in range(4)]
    slot_null_rates = [_null_rate(slot) for slot in slots]
    if all(rate >= 0.6 for rate in slot_null_rates[:3]) and slot_null_rates[3] < 0.3:
        return "utf-32-be"
    if slot_null_rates[0] < 0.3 and all(rate >= 0.6 for rate in slot_null_rates[1:]):
        return "utf-32-le"

    even_null_rate = _null_rate(sample[0::2])
    odd_null_rate = _null_rate(sample[1::2])
    if even_null_rate >= 0.6 and odd_null_rate < 0.3:
        return "utf-16-be"
    if odd_null_rate >= 0.6 and even_null_rate < 0.3:
        return "utf-16-le"
    return None


def _null_rate(data: bytes) -> float:
    if not data:
        return 0.0
    return data.count(0) / len(data)


def _declared_xml_encoding(text: str) -> str | None:
    match = _XML_ENCODING_DECLARATION.search(text[:512])
    if not match:
        return None
    encoding = match.group("encoding").strip()
    try:
        codecs.lookup(encoding)
    except LookupError:
        return None
    return encoding


def _decode_docx_xml_for_scan(xml: bytes | str) -> list[str]:
    if isinstance(xml, str):
        return [xml]

    encodings = [_sniff_xml_encoding(xml) or "utf-8"]
    text = xml.decode(encodings[0], "replace")
    declared_encoding = _declared_xml_encoding(text)
    if declared_encoding:
        encodings.append(declared_encoding)

    texts = []
    seen = set()
    for encoding in encodings:
        if encoding in seen:
            continue
        seen.add(encoding)
        texts.append(xml.decode(encoding, "replace"))
    return texts


def reject_unsafe_docx_xml(xml: bytes | str, *, part_name: str = "XML part") -> None:
    for text in _decode_docx_xml_for_scan(xml):
        if _UNSAFE_XML_DECLARATION.search(text):
            raise UnsafeDocxXmlError(f"{part_name}: {UNSAFE_XML_DECLARATION_MESSAGE}")


def parse_docx_xml(xml: bytes | str, *, part_name: str = "XML part") -> ET.Element:
    reject_unsafe_docx_xml(xml, part_name=part_name)
    return ET.fromstring(xml)


# --- Shared WordprocessingML namespaces and low-level XML helpers ---
# Hoisted out of docx_export so docx_export, docx_comments, and redline_xml can
# all depend on this module as the dependency root without import cycles. These
# are behaviour-preserving moves; the implementations are unchanged.

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

ET.register_namespace("w", W_NS)
ET.register_namespace("r", R_NS)

# ``ET.register_namespace`` mutates a PROCESS-GLOBAL map (``ET._namespace_map``).
# The two WordprocessingML prefixes above are registered ONCE at import and never
# change, which is safe. But the OPC package parts (``[Content_Types].xml``,
# ``_rels/.rels``, ``word/_rels/document.xml.rels``) use their namespace as the
# UNPREFIXED default -- and their elements carry non-namespaced attributes
# (``Id``/``Type``/``Target``/``PartName``/...), which makes ElementTree's
# ``default_namespace=`` serialization argument raise. The only way to emit
# ``xmlns="..."`` (default) for those roots is to register the empty prefix for
# their URI, which must be done on the global map. To keep that global mutation
# from (a) racing with concurrent reviewed-DOCX -> PDF conversions (the app is
# threaded and runs conversions 2-concurrently) and (b) leaking across calls, we
# register under a lock and restore SURGICALLY -- undoing only the exact entries
# our ``register_namespace`` touched -- for the duration of a single
# serialization. See ``_default_namespace_registration``.
#
# This lock is shared with ``docx_image_normalize._serialize_xml`` (which routes
# its own empty-prefix registration through this same context manager), so the
# two families of OPC serialization (redline OPC parts vs image-normalize
# content-types) never mutate ``ET._namespace_map`` concurrently. It is a plain
# (non-reentrant) ``Lock`` on purpose: no code path nests a second registration
# inside a held one. If that ever changes, make it an ``RLock``.
_NAMESPACE_MAP_LOCK = threading.Lock()


@contextlib.contextmanager
def _default_namespace_registration(uri: str) -> Iterator[None]:
    """Temporarily register ``uri`` as the DEFAULT (unprefixed) namespace, then
    restore the global ``ET._namespace_map`` SURGICALLY in a ``finally``.

    Held under ``_NAMESPACE_MAP_LOCK`` so the register -> serialize -> restore
    cycle is atomic with respect to any other thread doing the same thing: two
    concurrent callers registering the empty prefix for DIFFERENT URIs can never
    observe each other's half-registered state, and the map is guaranteed to be
    returned to exactly its prior contents even if serialization raises.

    The restore does NOT ``clear()`` the map. ``clear()`` would momentarily EMPTY
    the entire process-global map between two bytecode ops, and any *unlocked*
    ``ET.register_namespace`` running concurrently (e.g. ``_register_xml_namespaces``
    while parsing another document) iterates a snapshot of the keys and does
    ``del _namespace_map[k]`` -- if ``clear()`` removed those keys first that
    raises ``KeyError`` (a 500 on the live serve paths). Instead we record exactly
    the entries ``register_namespace("", uri)`` deletes (every entry whose key is
    ``uri`` or whose value is the empty prefix) and reinstate only those, plus we
    drop the ``uri -> ""`` mapping we introduced. Pre-existing, unrelated entries
    are never removed at any observable moment.
    """
    with _NAMESPACE_MAP_LOCK:
        namespace_map = ET._namespace_map
        # ``register_namespace("", uri)`` deletes every entry whose key == uri or
        # whose value == "" (the empty prefix), then sets ``namespace_map[uri] = ""``.
        # Capture precisely that deleted set so we can put it back untouched.
        removed = {k: v for k, v in namespace_map.items() if k == uri or v == ""}
        try:
            ET.register_namespace("", uri)
            yield
        finally:
            # Drop the ``uri -> ""`` mapping we added (unless ``uri`` had a prior
            # value, in which case the re-add below restores it), then reinstate
            # exactly the entries register_namespace removed. Never ``clear()``.
            if uri not in removed:
                namespace_map.pop(uri, None)
            namespace_map.update(removed)

INVALID_XML_CHAR_PATTERN = re.compile(
    "[\x00-\x08\x0B\x0C\x0E-\x1F"
    "\uD800-\uDFFF"
    "﷐-﷯"
    "￾￿"
    "\U0001FFFE\U0001FFFF"
    "\U0002FFFE\U0002FFFF"
    "\U0003FFFE\U0003FFFF"
    "\U0004FFFE\U0004FFFF"
    "\U0005FFFE\U0005FFFF"
    "\U0006FFFE\U0006FFFF"
    "\U0007FFFE\U0007FFFF"
    "\U0008FFFE\U0008FFFF"
    "\U0009FFFE\U0009FFFF"
    "\U000AFFFE\U000AFFFF"
    "\U000BFFFE\U000BFFFF"
    "\U000CFFFE\U000CFFFF"
    "\U000DFFFE\U000DFFFF"
    "\U000EFFFE\U000EFFFF"
    "\U000FFFFE\U000FFFFF"
    "\U0010FFFE\U0010FFFF]"
)
RESERVED_NAMESPACE_PREFIX_PATTERN = re.compile(r"ns\d+$")
XML_NAMESPACE_PREFIX_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _escape_xml(value: str) -> str:
    return (
        _strip_invalid_xml_chars(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _escape_attr(value: str) -> str:
    return _escape_xml(value)


def _strip_invalid_xml_chars(value: object) -> str:
    return INVALID_XML_CHAR_PATTERN.sub("", str(value))


def _w_tag(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _rel_tag(tag: str) -> str:
    return f"{{{REL_NS}}}{tag}"


def _content_type_tag(tag: str) -> str:
    return f"{{{CONTENT_TYPES_NS}}}{tag}"


def _normalize_paragraph_text(value: object) -> str:
    # Fold ligatures BEFORE whitespace collapse so a ligature-bearing paragraph
    # normalizes identically to its ASCII-spelled twin -- the PDF->DOCX anchor
    # mapping compares both sides through this function, so folding here keeps a
    # ligature PDF from failing to map (and being refused as an empty working DOCX).
    return re.sub(r"\s+", " ", fold_ligatures(str(value or ""))).strip()


def _word_paragraph_from_xml(paragraph_xml: str) -> ET.Element:
    wrapper = parse_docx_xml(f'<root xmlns:w="{W_NS}">{paragraph_xml}</root>', part_name="redline paragraph")
    return wrapper[0]


def _paragraph_text(paragraph: ET.Element) -> str:
    parts = []
    for node in paragraph.iter():
        if node.tag == _w_tag("t") and node.text:
            parts.append(node.text)
        elif node.tag == _w_tag("tab"):
            parts.append("\t")
        elif node.tag in {_w_tag("br"), _w_tag("cr")}:
            parts.append("\n")
    return "".join(parts).strip()


def _parse_docx_xml_with_namespaces(xml: bytes, *, part_name: str) -> tuple[ET.Element, Dict[str, str]]:
    root = parse_docx_xml(xml, part_name=part_name)
    namespaces = _xml_namespace_declarations(xml)
    _register_xml_namespaces(namespaces)
    return root, namespaces


def _xml_namespace_declarations(xml: bytes) -> Dict[str, str]:
    namespaces: Dict[str, str] = {}
    for _event, namespace in ET.iterparse(BytesIO(xml), events=("start-ns",)):
        prefix, uri = namespace
        if prefix and uri and prefix not in namespaces:
            namespaces[prefix] = uri
    return namespaces


def _register_xml_namespaces(namespaces: Dict[str, str]) -> None:
    # ``ET.register_namespace`` mutates the PROCESS-GLOBAL ``ET._namespace_map``:
    # it snapshots the items, deletes every matching key, then re-adds. Run
    # UNLOCKED it races ``_default_namespace_registration`` (which mutates the same
    # map under ``_NAMESPACE_MAP_LOCK``): the concurrent restore can delete a key
    # this loop's snapshot still lists, so the internal ``del`` raises KeyError -- a
    # rare (~1/30000 under adversarial key collision) 500 on the live serve paths.
    # Serialize both mutators on the one lock. Deadlock-safe: this is never called
    # from inside a held ``_NAMESPACE_MAP_LOCK`` (that lock only ever wraps
    # ``ET.tostring``; this runs on PARSE paths), and the lock is not re-entered.
    # Output is unchanged -- the SAME prefixes are registered, just not concurrently
    # with a restore.
    with _NAMESPACE_MAP_LOCK:
        for prefix, uri in namespaces.items():
            if not _can_preserve_namespace_prefix(prefix):
                continue
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                continue


def _can_preserve_namespace_prefix(prefix: str) -> bool:
    if not prefix or prefix in {"xml", "xmlns"}:
        return False
    if RESERVED_NAMESPACE_PREFIX_PATTERN.fullmatch(prefix):
        return False
    return bool(XML_NAMESPACE_PREFIX_PATTERN.fullmatch(prefix))


def _xml_bytes(
    root: ET.Element,
    *,
    namespace_declarations: Dict[str, str] | None = None,
    default_namespace: str | None = None,
) -> bytes:
    """Serialize ``root`` to UTF-8 XML bytes.

    ``default_namespace`` (used for OPC package parts) makes that URI serialize as
    the UNPREFIXED default (``xmlns="..."``) instead of an ElementTree-invented
    ``ns0:`` prefix, matching the canonical shape the source packages use and that
    both Word and LibreOffice accept. When it is ``None`` the serialization path is
    byte-for-byte identical to before -- the WordprocessingML ``document.xml`` etc.
    take this path and are unaffected.
    """
    _strip_invalid_xml_chars_from_tree(root)
    if default_namespace:
        with _default_namespace_registration(default_namespace):
            xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    else:
        xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if namespace_declarations:
        xml = _ensure_root_namespace_declarations(xml, namespace_declarations)
    return xml


def _strip_invalid_xml_chars_from_tree(root: ET.Element) -> None:
    for element in root.iter():
        if element.text:
            element.text = _strip_invalid_xml_chars(element.text)
        if element.tail:
            element.tail = _strip_invalid_xml_chars(element.tail)
        for key, value in list(element.attrib.items()):
            element.attrib[key] = _strip_invalid_xml_chars(value)


def _ensure_root_namespace_declarations(xml: bytes, namespace_declarations: Dict[str, str]) -> bytes:
    root_start, root_end = _root_start_tag_bounds(xml)
    if root_start is None or root_end is None:
        return xml

    root_tag = xml[root_start:root_end]
    missing_declarations = []
    for prefix, uri in namespace_declarations.items():
        if not _can_preserve_namespace_prefix(prefix):
            continue
        declaration_name = f"xmlns:{prefix}=".encode("utf-8")
        if declaration_name in root_tag:
            continue
        escaped_uri = _escape_attr(uri).encode("utf-8")
        missing_declarations.append(b' xmlns:' + prefix.encode("utf-8") + b'="' + escaped_uri + b'"')

    if not missing_declarations:
        return xml
    insertion_point = root_end - 1 if xml[root_end - 1:root_end] == b"/" else root_end
    return xml[:insertion_point] + b"".join(missing_declarations) + xml[insertion_point:]


def _root_start_tag_bounds(xml: bytes) -> tuple[int | None, int | None]:
    offset = 0
    if xml.startswith(b"<?xml"):
        declaration_end = xml.find(b"?>")
        if declaration_end != -1:
            offset = declaration_end + 2

    root_start = xml.find(b"<", offset)
    if root_start == -1:
        return None, None
    root_end = xml.find(b">", root_start)
    if root_end == -1:
        return None, None
    return root_start, root_end


def _clone_element(element: ET.Element) -> ET.Element:
    return parse_docx_xml(ET.tostring(element, encoding="utf-8"), part_name="cloned XML")
