"""Pin the byte SHAPE of the OPC package parts the redline builder rewrites.

Regression guard for the LibreOffice-rejection defect: when
``_build_source_redline_docx_package`` reserialized ``[Content_Types].xml``,
``_rels/.rels`` and ``word/_rels/document.xml.rels``, ElementTree invented an
``ns0:`` prefix (``<ns0:Relationships xmlns:ns0="...">``) instead of the default
namespace the originals use (``<Relationships xmlns="...">``). Word and
python-docx tolerate the prefix, but LibreOffice 26.2 rejects every such file
("source file could not be loaded"), which breaks the reviewed/redlined
DOCX -> PDF conversion the app runs via LibreOffice (reviewed-pdf endpoint and
DocuSign e-signature). The fix registers the empty prefix for the OPC namespace
for the duration of each serialization, under a lock, restoring the global
``ET._namespace_map`` afterwards. These tests lock in:

  (a) the three OPC parts serialize with the DEFAULT namespace, never ``ns0:``;
  (b) the WordprocessingML ``document.xml`` (w:/r:) path is byte-UNCHANGED;
  (c) a real redlined package opens via python-docx with no ``ns0:`` anywhere
      and an intact part list / content types;
  (d) concurrent serialization of DIFFERENT OPC part types never cross-
      contaminates and never leaves the global namespace map mutated.
"""

from __future__ import annotations

import threading
import unittest
import xml.etree.ElementTree as ET
from io import BytesIO
from zipfile import ZipFile

import docx  # python-docx

from nda_automation.docx_xml import (
    CONTENT_TYPES_NS,
    REL_NS,
    W_NS as W_NS_URI,
    _default_namespace_registration,
    _w_tag,
    _xml_bytes,
)
from nda_automation.docx_export import (
    _content_types_xml_with_settings,
    _document_rels_xml_with_settings,
    _package_rels_xml_with_document,
    build_source_redline_docx,
)
from nda_automation.docx_text import extract_docx_paragraphs
from nda_automation.checker import review_nda
from tests.test_docx_export import make_source_docx


def _source_part(source_docx: bytes, part_name: str) -> bytes:
    with ZipFile(BytesIO(source_docx)) as archive:
        return archive.read(part_name)


class OpcDefaultNamespaceSerializationTests(unittest.TestCase):
    # ---- (a) OPC parts serialize with the DEFAULT namespace, never ns0: ----

    def _assert_default_namespace(self, xml: bytes, namespace: str) -> None:
        text = xml.decode("utf-8")
        self.assertIn(f'xmlns="{namespace}"', text)
        self.assertNotIn("ns0:", text)
        self.assertNotIn("xmlns:ns", text)

    def test_document_rels_serialize_with_default_namespace(self):
        # Both the synthetic (None) and the real parse->reserialize path.
        for relationships_xml in (None, _make_document_rels_bytes()):
            xml = _document_rels_xml_with_settings(relationships_xml, has_comments=True)
            self._assert_default_namespace(xml, REL_NS)
            self.assertIn("settings.xml", xml.decode("utf-8"))

    def test_package_rels_serialize_with_default_namespace(self):
        source = make_source_docx(["Anchor."])
        for relationships_xml in (None, _source_part(source, "_rels/.rels")):
            xml = _package_rels_xml_with_document(relationships_xml)
            self._assert_default_namespace(xml, REL_NS)
            self.assertIn("word/document.xml", xml.decode("utf-8"))

    def test_content_types_serialize_with_default_namespace(self):
        source = make_source_docx(["Anchor."])
        for content_types_xml in (None, _source_part(source, "[Content_Types].xml")):
            xml = _content_types_xml_with_settings(content_types_xml, has_styles=True, has_comments=True)
            self._assert_default_namespace(xml, CONTENT_TYPES_NS)
            # The content-types namespace must be the default, never mislabelled
            # with the relationships namespace.
            self.assertNotIn(f'xmlns="{REL_NS}"', xml.decode("utf-8"))

    # ---- (b) the WordprocessingML document.xml path is byte-UNCHANGED ----

    def test_document_xml_serialization_is_byte_identical(self):
        # Golden captured from origin/main's _xml_bytes for the same controlled
        # w: tree. The default_namespace=None path must remain byte-for-byte
        # identical: w:/r: keep their prefixes, no OPC-style default namespace.
        golden = (
            b"<?xml version='1.0' encoding='utf-8'?>\n"
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p><w:r><w:t>Hello &amp; &lt;world&gt;</w:t></w:r></w:p></w:body>"
            b"</w:document>"
        )
        root = _build_w_document_tree()
        produced = _xml_bytes(root, namespace_declarations={"w": W_NS_URI})
        self.assertEqual(produced, golden)
        # Passing default_namespace=None explicitly must not change anything.
        again = _xml_bytes(
            _build_w_document_tree(),
            namespace_declarations={"w": W_NS_URI},
            default_namespace=None,
        )
        self.assertEqual(again, golden)
        self.assertNotIn(b"ns0:", produced)

    # ---- (c) a REAL redlined package opens via python-docx, no ns0: ----

    def test_real_redline_package_has_default_namespace_opc_parts(self):
        source = make_source_docx([
            "Intro paragraph.",
            "This Agreement shall be governed by the laws of California.",
            "The confidentiality obligations survive for three years.",
        ])
        paragraphs = extract_docx_paragraphs(source)
        result = review_nda(
            "\n\n".join(str(p["text"]) for p in paragraphs), paragraphs=paragraphs
        )
        redlined = build_source_redline_docx(source, result)

        # Opens via python-docx (the whole package is validated on open).
        docx.Document(BytesIO(redlined))

        opc_parts = {
            "word/_rels/document.xml.rels": REL_NS,
            "_rels/.rels": REL_NS,
            "[Content_Types].xml": CONTENT_TYPES_NS,
        }
        with ZipFile(BytesIO(redlined)) as archive:
            names = set(archive.namelist())
            # Part list intact: the rewrites must not drop or duplicate parts.
            self.assertEqual(len(archive.namelist()), len(names))
            for required in (*opc_parts, "word/document.xml", "customXml/item1.xml"):
                self.assertIn(required, names)
            for part_name, namespace in opc_parts.items():
                self._assert_default_namespace(archive.read(part_name), namespace)
            # No ns0: leaked into ANY xml/rels part of the package.
            for name in names:
                if name.endswith(".xml") or name.endswith(".rels"):
                    self.assertNotIn(b"ns0:", archive.read(name), f"ns0: leaked into {name}")
            # Content types are intact and still describe the document part.
            content_types = archive.read("[Content_Types].xml").decode("utf-8")
            self.assertIn('PartName="/word/document.xml"', content_types)
            self.assertIn('Extension="rels"', content_types)

    # ---- (d) thread-safety: no cross-contamination, no global-map leak ----

    def test_concurrent_serialization_does_not_race_on_global_namespace_map(self):
        source = make_source_docx(["Anchor."])
        rels_source = _source_part(source, "_rels/.rels")
        content_types_source = _source_part(source, "[Content_Types].xml")

        # Snapshot the process-global namespace map: the scoped registration must
        # restore it EXACTLY, regardless of any pre-existing (foreign) entries.
        map_before = dict(ET._namespace_map)

        errors: list[AssertionError] = []
        barrier = threading.Barrier(2)

        def hammer_relationships() -> None:
            barrier.wait()
            for _ in range(200):
                xml = _package_rels_xml_with_document(rels_source).decode("utf-8")
                if (
                    f'xmlns="{REL_NS}"' not in xml
                    or "ns0:" in xml
                    or f'xmlns="{CONTENT_TYPES_NS}"' in xml  # cross-contamination
                ):
                    errors.append(AssertionError(f"relationships output corrupted: {xml[:120]}"))
                    return

        def hammer_content_types() -> None:
            barrier.wait()
            for _ in range(200):
                xml = _content_types_xml_with_settings(
                    content_types_source, has_styles=True, has_comments=True
                ).decode("utf-8")
                if (
                    f'xmlns="{CONTENT_TYPES_NS}"' not in xml
                    or "ns0:" in xml
                    or f'xmlns="{REL_NS}"' in xml  # cross-contamination
                ):
                    errors.append(AssertionError(f"content-types output corrupted: {xml[:120]}"))
                    return

        threads = [
            threading.Thread(target=hammer_relationships),
            threading.Thread(target=hammer_content_types),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [], errors[0].args[0] if errors else "")
        # The global map must be exactly what it was before -- no leaked "" entry,
        # no leaked OPC-namespace mapping.
        self.assertEqual(dict(ET._namespace_map), map_before)


class DefaultNamespaceRegistrationSurgicalRestoreTests(unittest.TestCase):
    """``_default_namespace_registration`` must restore ``ET._namespace_map``
    SURGICALLY -- undoing only the entries its ``register_namespace`` touched --
    and must NEVER ``clear()`` the whole map.

    A bulk ``clear()`` momentarily EMPTIES the process-global map. The adversarial
    gate proved that empties window crashes a concurrent *unlocked*
    ``register_namespace`` (its internal ``del _namespace_map[k]`` loop hits
    ``KeyError``) -- a 500 on the live reviewed-docx / matters-download serve paths
    -- and can leak an ``ns0:`` prefix onto an unrelated serialization mid-window.
    """

    def test_restore_never_calls_clear_on_the_global_map(self):
        class _NoClearDict(dict):
            def clear(self):  # type: ignore[override]
                raise AssertionError(
                    "_default_namespace_registration must not clear() the global namespace map"
                )

        original = ET._namespace_map
        ET._namespace_map = _NoClearDict(original)
        try:
            with _default_namespace_registration(CONTENT_TYPES_NS):
                self.assertEqual(ET._namespace_map.get(CONTENT_TYPES_NS), "")
        finally:
            restored = dict(ET._namespace_map)
            ET._namespace_map = original
            original.clear()
            original.update(restored)
        # The empty-prefix mapping we introduced is gone; nothing leaked.
        self.assertNotEqual(ET._namespace_map.get(CONTENT_TYPES_NS), "")

    def test_prior_prefix_for_same_uri_is_restored_exactly(self):
        map_before = dict(ET._namespace_map)
        sentinel_uri = "urn:nda-test:surgical-restore-same-uri"
        ET.register_namespace("sret", sentinel_uri)  # a prior prefix we must not lose
        try:
            with _default_namespace_registration(sentinel_uri):
                self.assertEqual(ET._namespace_map.get(sentinel_uri), "")
            # The prior prefix for this exact uri is restored, not dropped.
            self.assertEqual(ET._namespace_map.get(sentinel_uri), "sret")
        finally:
            ET._namespace_map.pop(sentinel_uri, None)
        self.assertEqual(dict(ET._namespace_map), map_before)

    def test_unrelated_entry_never_absent_during_concurrent_restore(self):
        # A pre-existing entry UNRELATED to the registered uri (different key,
        # non-empty prefix) must never be observed missing while another thread
        # runs the register/restore cycle. Under the old bulk-clear restore a
        # reader would catch it absent; under surgical restore it is untouched.
        map_before = dict(ET._namespace_map)
        sentinel_uri = "urn:nda-test:sentinel-never-absent"
        ET.register_namespace("nvr", sentinel_uri)
        missing_observed: list[bool] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                if sentinel_uri not in ET._namespace_map:
                    missing_observed.append(True)

        def writer() -> None:
            for _ in range(3000):
                with _default_namespace_registration(CONTENT_TYPES_NS):
                    pass

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            writer()
        finally:
            stop.set()
            reader_thread.join()
        try:
            self.assertEqual(
                missing_observed,
                [],
                "a pre-existing unrelated entry disappeared from ET._namespace_map during restore",
            )
        finally:
            ET._namespace_map.pop(sentinel_uri, None)
        self.assertEqual(dict(ET._namespace_map), map_before)

    def test_map_restored_when_body_raises(self):
        map_before = dict(ET._namespace_map)
        with self.assertRaises(RuntimeError):
            with _default_namespace_registration(CONTENT_TYPES_NS):
                raise RuntimeError("boom")
        self.assertEqual(dict(ET._namespace_map), map_before)


def _build_w_document_tree() -> ET.Element:
    root = ET.Element(_w_tag("document"))
    body = ET.SubElement(root, _w_tag("body"))
    paragraph = ET.SubElement(body, _w_tag("p"))
    run = ET.SubElement(paragraph, _w_tag("r"))
    text = ET.SubElement(run, _w_tag("t"))
    text.text = "Hello & <world>"
    return root


def _make_document_rels_bytes() -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        b'Target="styles.xml"/>'
        b"</Relationships>"
    )


if __name__ == "__main__":
    unittest.main()
