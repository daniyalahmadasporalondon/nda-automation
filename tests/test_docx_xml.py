import threading
import unittest
import xml.etree.ElementTree as ET

from nda_automation.docx_xml import (
    UnsafeDocxXmlError,
    _normalize_paragraph_text,
    _register_xml_namespaces,
    _xml_bytes,
    fold_ligatures,
    parse_docx_xml,
    reject_unsafe_docx_xml,
)


class DocxXmlTests(unittest.TestCase):
    def test_rejects_utf16_bom_dtd_entity_declarations(self):
        data = unsafe_xml_part("UTF-16").encode("utf-16")

        with self.assertRaisesRegex(UnsafeDocxXmlError, "word/document.xml"):
            reject_unsafe_docx_xml(data, part_name="word/document.xml")

    def test_rejects_utf16le_dtd_entity_declarations_without_bom(self):
        data = unsafe_xml_part("UTF-16LE").encode("utf-16-le")

        with self.assertRaisesRegex(UnsafeDocxXmlError, "word/header1.xml"):
            reject_unsafe_docx_xml(data, part_name="word/header1.xml")

    def test_rejects_utf32be_dtd_entity_declarations_without_bom(self):
        data = unsafe_xml_part("UTF-32BE").encode("utf-32-be")

        with self.assertRaisesRegex(UnsafeDocxXmlError, "word/footer1.xml"):
            reject_unsafe_docx_xml(data, part_name="word/footer1.xml")

    def test_parses_safe_utf16_xml_parts(self):
        data = safe_xml_part("UTF-16").encode("utf-16")

        root = parse_docx_xml(data, part_name="word/document.xml")

        self.assertEqual(root.tag, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document")


def unsafe_xml_part(encoding):
    return f"""<?xml version="1.0" encoding="{encoding}"?>
<!DOCTYPE w:document [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body>
</w:document>"""


def safe_xml_part(encoding):
    return f"""<?xml version="1.0" encoding="{encoding}"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Safe body text.</w:t></w:r></w:p></w:body>
</w:document>"""


class RegisterXmlNamespacesConcurrencyTests(unittest.TestCase):
    """`_register_xml_namespaces` (parse time) and `_default_namespace_registration`
    (serialize time) both mutate the PROCESS-GLOBAL `ET._namespace_map`.
    `ET.register_namespace` snapshots the map, deletes matching keys, then re-adds;
    run unlocked, a concurrent scoped restore can delete a key the snapshot still
    lists, so the internal `del` raises KeyError -- a rare 500 on the serve path.
    The fix routes `_register_xml_namespaces` through the shared
    `_NAMESPACE_MAP_LOCK`. This hammers both on the SAME colliding uri: no
    exception may escape, the threads must not deadlock (the lock is not
    re-entered), and pre-existing map entries stay intact."""

    def test_no_keyerror_or_deadlock_under_colliding_registration(self):
        import sys

        collide_uri = "urn:collide-k-fix"
        pre = dict(ET._namespace_map)
        iterations = 20000
        # Force very frequent thread switches so the tiny snapshot/mutate window in
        # ``ET.register_namespace`` is actually straddled. Without the lock this
        # reliably raises ("dictionary changed size during iteration" / KeyError);
        # with it, the mutators are serialized and it is clean. Restored in finally.
        prev_switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        barrier = threading.Barrier(2, timeout=30)
        errors: list[str] = []
        errors_lock = threading.Lock()

        def record(msg: str) -> None:
            with errors_lock:
                errors.append(msg)

        def parse_side() -> None:
            for _ in range(iterations):
                try:
                    barrier.wait()
                    # Parse-time registration -- permanent, unscoped.
                    _register_xml_namespaces({"shared": collide_uri})
                except threading.BrokenBarrierError:
                    return
                except Exception as exc:  # the KeyError the fix prevents
                    record(f"parse-side {type(exc).__name__}: {exc}")

        def serialize_side() -> None:
            root = ET.Element(f"{{{collide_uri}}}Root")
            for _ in range(iterations):
                try:
                    barrier.wait()
                    # Serialize-time scoped register/restore of the empty prefix.
                    _xml_bytes(root, default_namespace=collide_uri)
                except threading.BrokenBarrierError:
                    return
                except Exception as exc:
                    record(f"serialize-side {type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=parse_side),
            threading.Thread(target=serialize_side),
        ]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=90)
            for t in threads:
                self.assertFalse(
                    t.is_alive(),
                    "a thread never finished -- deadlock (was _NAMESPACE_MAP_LOCK "
                    "re-entered from a held lock?)",
                )
            self.assertEqual(errors, [], f"race errors under concurrency: {errors[:3]}")
            # Pre-existing, unrelated registrations (w/r/...) are never touched.
            for key, value in pre.items():
                self.assertEqual(ET._namespace_map.get(key), value)
            # No empty-prefix default leaked for the collide uri.
            self.assertNotEqual(ET._namespace_map.get(collide_uri), "")
        finally:
            sys.setswitchinterval(prev_switch_interval)
            # Parse-time registration persists by design; clean it so the global
            # map is left as we found it for other tests.
            ET._namespace_map.pop(collide_uri, None)


class LigatureFoldingTests(unittest.TestCase):
    def test_fold_ligatures_maps_each_presentation_form(self):
        self.assertEqual(fold_ligatures("Conﬁdential"), "Confidential")  # ﬁ
        self.assertEqual(fold_ligatures("Inﬂuence"), "Influence")  # ﬂ
        self.assertEqual(fold_ligatures("Oﬀer"), "Offer")  # ﬀ
        self.assertEqual(fold_ligatures("Aﬃliate"), "Affiliate")  # ﬃ
        self.assertEqual(fold_ligatures("baﬄe"), "baffle")  # ﬄ

    def test_fold_ligatures_is_noop_and_idempotent_for_ascii(self):
        self.assertEqual(fold_ligatures("Confidential"), "Confidential")
        self.assertEqual(fold_ligatures(""), "")
        once = fold_ligatures("Conﬁdential ﬂow")
        self.assertEqual(fold_ligatures(once), once)  # idempotent

    def test_normalize_paragraph_text_folds_before_whitespace_collapse(self):
        self.assertEqual(
            _normalize_paragraph_text("Conﬁdential   \n Inﬂuence"),
            "Confidential Influence",
        )


if __name__ == "__main__":
    unittest.main()
