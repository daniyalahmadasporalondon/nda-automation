from __future__ import annotations

import codecs
import re
import xml.etree.ElementTree as ET

UNSAFE_XML_DECLARATION_MESSAGE = "The Word document contains unsupported XML DTD/entity declarations."
_UNSAFE_XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_ENCODING_DECLARATION = re.compile(
    r"^\ufeff?\s*<\?xml\s+[^>]*\bencoding\s*=\s*(['\"])(?P<encoding>[^'\"]+)\1",
    re.IGNORECASE,
)


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
