from __future__ import annotations

from io import BytesIO
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from nda_automation.docx_image_normalize import (
    CONTENT_TYPES_NS,
    PLACEHOLDER_PNG_BYTES,
    PNG_CONTENT_TYPE,
    RELATIONSHIPS_NS,
    normalize_docx_emf_wmf_images,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _build_docx(*, media: dict[str, bytes], content_types: bytes, document_rels: bytes) -> bytes:
    """Assemble a minimal but structurally valid DOCX archive."""
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr(
            "_rels/.rels",
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            b'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            b'<?xml version="1.0"?>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p/></w:body></w:document>",
        )
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        for name, data in media.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _emf_docx() -> bytes:
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Default Extension="emf" ContentType="image/x-emf"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b"</Types>"
    )
    document_rels = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId5" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        b'Target="media/image1.emf"/></Relationships>'
    )
    return _build_docx(
        media={"word/media/image1.emf": b"EMF-FAKE-VECTOR-BYTES"},
        content_types=content_types,
        document_rels=document_rels,
    )


def _png_data_url_docx() -> bytes:
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Default Extension="png" ContentType="image/png"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b"</Types>"
    )
    document_rels = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId5" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        b'Target="media/logo.png"/></Relationships>'
    )
    return _build_docx(
        media={"word/media/logo.png": PNG_SIGNATURE + b"realpng"},
        content_types=content_types,
        document_rels=document_rels,
    )


def _read_member(docx_bytes: bytes, name: str) -> bytes:
    with ZipFile(BytesIO(docx_bytes)) as archive:
        return archive.read(name)


def _names(docx_bytes: bytes) -> list[str]:
    with ZipFile(BytesIO(docx_bytes)) as archive:
        return archive.namelist()


class FakeConverter:
    def __init__(self, png_payload: bytes):
        self.png_payload = png_payload
        self.calls: list[str] = []

    def __call__(self, data: bytes, extension: str) -> bytes:
        self.calls.append(extension)
        return self.png_payload


class NormalizeEmfWmfTests(unittest.TestCase):
    def test_emf_part_becomes_png_with_converter(self):
        fake_png = PNG_SIGNATURE + b"converted-logo"
        converter = FakeConverter(fake_png)
        result = normalize_docx_emf_wmf_images(_emf_docx(), converter=converter)

        names = _names(result)
        self.assertNotIn("word/media/image1.emf", names)
        self.assertIn("word/media/image1.png", names)
        self.assertEqual(_read_member(result, "word/media/image1.png"), fake_png)
        self.assertEqual(converter.calls, ["emf"])

    def test_relationship_target_is_rewritten(self):
        converter = FakeConverter(PNG_SIGNATURE + b"x")
        result = normalize_docx_emf_wmf_images(_emf_docx(), converter=converter)

        rels = _read_member(result, "word/_rels/document.xml.rels")
        root = ET.fromstring(rels)
        targets = {
            rel.get("Id"): rel.get("Target")
            for rel in root.iter(f"{{{RELATIONSHIPS_NS}}}Relationship")
        }
        # rId stays stable (so <a:blip r:embed="rId5"> still resolves); only the
        # Target extension flips to png.
        self.assertEqual(targets["rId5"], "media/image1.png")

    def test_content_types_declares_png_and_drops_emf(self):
        converter = FakeConverter(PNG_SIGNATURE + b"x")
        result = normalize_docx_emf_wmf_images(_emf_docx(), converter=converter)

        content_types = _read_member(result, "[Content_Types].xml")
        root = ET.fromstring(content_types)
        defaults = {
            child.get("Extension", "").lower(): child.get("ContentType")
            for child in root
            if child.tag == f"{{{CONTENT_TYPES_NS}}}Default"
        }
        self.assertEqual(defaults.get("png"), PNG_CONTENT_TYPE)
        self.assertNotIn("emf", defaults)

    def test_placeholder_substituted_when_no_tool(self):
        # converter returns None -> guaranteed placeholder PNG, never a blank box.
        result = normalize_docx_emf_wmf_images(_emf_docx(), converter=lambda data, ext: None)
        png = _read_member(result, "word/media/image1.png")
        self.assertEqual(png, PLACEHOLDER_PNG_BYTES)
        self.assertTrue(png.startswith(PNG_SIGNATURE))

    def test_non_png_converter_output_falls_back_to_placeholder(self):
        # A converter that returns junk (not a PNG) must not poison the part.
        result = normalize_docx_emf_wmf_images(_emf_docx(), converter=lambda data, ext: b"not-a-png")
        self.assertEqual(_read_member(result, "word/media/image1.png"), PLACEHOLDER_PNG_BYTES)

    def test_docx_without_vector_images_is_unchanged(self):
        original = _png_data_url_docx()
        result = normalize_docx_emf_wmf_images(original, converter=FakeConverter(b"unused"))
        # No EMF/WMF -> returned byte-identical (and the same object's bytes).
        self.assertEqual(result, original)

    def test_invalid_zip_returned_untouched(self):
        junk = b"this is not a docx"
        self.assertEqual(normalize_docx_emf_wmf_images(junk), junk)

    def test_result_is_still_a_readable_zip(self):
        converter = FakeConverter(PNG_SIGNATURE + b"x")
        result = normalize_docx_emf_wmf_images(_emf_docx(), converter=converter)
        with ZipFile(BytesIO(result)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("word/document.xml", archive.namelist())

    def test_wmf_extension_also_normalized(self):
        content_types = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b'<Default Extension="wmf" ContentType="image/x-wmf"/>'
            b'<Override PartName="/word/document.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            b"</Types>"
        )
        document_rels = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId7" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            b'Target="media/image2.wmf"/></Relationships>'
        )
        docx_bytes = _build_docx(
            media={"word/media/image2.wmf": b"WMF-FAKE"},
            content_types=content_types,
            document_rels=document_rels,
        )
        converter = FakeConverter(PNG_SIGNATURE + b"y")
        result = normalize_docx_emf_wmf_images(docx_bytes, converter=converter)
        self.assertIn("word/media/image2.png", _names(result))
        self.assertNotIn("word/media/image2.wmf", _names(result))
        self.assertEqual(converter.calls, ["wmf"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
