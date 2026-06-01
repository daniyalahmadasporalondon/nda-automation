from __future__ import annotations

import re
import xml.etree.ElementTree as ET

UNSAFE_XML_DECLARATION_MESSAGE = "The Word document contains unsupported XML DTD/entity declarations."
_UNSAFE_XML_DECLARATION = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class UnsafeDocxXmlError(ValueError):
    """Raised when an OOXML part contains DTD/entity declarations."""


def is_docx_xml_part(part_name: str) -> bool:
    return part_name.endswith(".xml") or part_name.endswith(".rels")


def reject_unsafe_docx_xml(xml: bytes | str, *, part_name: str = "XML part") -> None:
    data = xml if isinstance(xml, bytes) else xml.encode("utf-8", "ignore")
    if _UNSAFE_XML_DECLARATION.search(data):
        raise UnsafeDocxXmlError(f"{part_name}: {UNSAFE_XML_DECLARATION_MESSAGE}")


def parse_docx_xml(xml: bytes | str, *, part_name: str = "XML part") -> ET.Element:
    reject_unsafe_docx_xml(xml, part_name=part_name)
    return ET.fromstring(xml)
