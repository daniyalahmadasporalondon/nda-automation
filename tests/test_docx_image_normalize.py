from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import nda_automation.docx_image_normalize as image_normalize
from nda_automation.docx_image_normalize import (
    CONTENT_TYPES_NS,
    PLACEHOLDER_PNG_BYTES,
    PNG_CONTENT_TYPE,
    RELATIONSHIPS_NS,
    _ImageConversionCache,
    _serialize_xml,
    normalize_docx_emf_wmf_images,
)
from nda_automation.docx_xml import _xml_bytes


def _docx_with_emf_media(media_by_name: dict[str, bytes]) -> bytes:
    """A DOCX whose ``word/media/*.emf`` parts are the given bytes (for cap /
    cache / concurrency tests that need several vector parts)."""
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
        b'Target="media/placeholder.emf"/></Relationships>'
    )
    return _build_docx(
        media=media_by_name, content_types=content_types, document_rels=document_rels
    )


def _media_png_names(docx_bytes: bytes) -> list[str]:
    return [n for n in _names(docx_bytes) if n.startswith("word/media/") and n.endswith(".png")]


def _media_emf_names(docx_bytes: bytes) -> list[str]:
    return [n for n in _names(docx_bytes) if n.startswith("word/media/") and n.endswith(".emf")]

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


class PlaceholderPngTests(unittest.TestCase):
    """The placeholder is the module's never-blank guarantee, so it must be a
    GENUINELY DECODABLE PNG -- not merely non-empty bytes. A malformed placeholder
    re-introduces the exact blank-box defect this module exists to prevent."""

    def test_placeholder_has_png_signature(self):
        self.assertTrue(PLACEHOLDER_PNG_BYTES.startswith(PNG_SIGNATURE))

    def test_placeholder_idat_zlib_stream_round_trips(self):
        import zlib

        idat_marker = PLACEHOLDER_PNG_BYTES.find(b"IDAT")
        self.assertGreater(idat_marker, 0)
        declared_length = int.from_bytes(
            PLACEHOLDER_PNG_BYTES[idat_marker - 4 : idat_marker], "big"
        )
        idat_data = PLACEHOLDER_PNG_BYTES[idat_marker + 4 : idat_marker + 4 + declared_length]
        self.assertEqual(len(idat_data), declared_length)
        # Must actually decompress (the old placeholder failed here with
        # "invalid stored block lengths").
        raw = zlib.decompress(idat_data)
        self.assertTrue(len(raw) > 0)

    def test_placeholder_opens_in_a_real_decoder(self):
        # Prefer PIL; fall back to PyMuPDF. If neither is installed, skip rather
        # than pass vacuously -- the point is a CONFORMANT decoder accepts it.
        opened = False
        try:
            from PIL import Image  # type: ignore[import-not-found]

            image = Image.open(BytesIO(PLACEHOLDER_PNG_BYTES))
            image.load()  # forces full decode, not just header parse
            self.assertEqual(image.size, (1, 1))
            opened = True
        except ImportError:
            pass
        if not opened:
            try:
                import fitz  # type: ignore[import-not-found]

                fitz.open(stream=PLACEHOLDER_PNG_BYTES, filetype="png")
                opened = True
            except ImportError:
                self.skipTest("no PNG decoder (PIL/PyMuPDF) available to validate placeholder")
        self.assertTrue(opened)


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


class SerializeXmlNamespaceLeakTests(unittest.TestCase):
    """``_serialize_xml`` registers the OPC namespace as the UNPREFIXED default on
    the PROCESS-GLOBAL ``ET._namespace_map``. It must do so through the scoped,
    lock-held snapshot/restore context manager so that (a) the global map never
    leaks across calls, (b) the map is restored even if serialization raises, and
    (c) concurrent serializations of DIFFERENT default namespaces (OPC
    relationships vs image-normalize's content-types ns) cannot contaminate each
    other. These tests are the regression guard for that fix."""

    # Frozen golden bytes captured from the pre-fix code on origin/main for the
    # _emf_docx() fixture. The safety fix MUST preserve these byte-for-byte for a
    # single-threaded call -- it changes global-state hygiene, not output shape.
    GOLDEN_CONTENT_TYPES = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />'
        b'<Default Extension="xml" ContentType="application/xml" />'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml" />'
        b'<Default Extension="png" ContentType="image/png" />'
        b"</Types>"
    )
    GOLDEN_DOCUMENT_RELS = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId5" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        b'Target="media/image1.png" /></Relationships>'
    )

    def test_golden_output_unchanged_vs_origin_main(self):
        # The whole DOCX round-trips to the exact bytes the pre-fix code produced:
        # unprefixed <Types>/<Relationships> roots, no invented ns0: prefix.
        result = normalize_docx_emf_wmf_images(
            _emf_docx(), converter=lambda data, ext: PNG_SIGNATURE + b"FAKEPNG"
        )
        self.assertEqual(_read_member(result, "[Content_Types].xml"), self.GOLDEN_CONTENT_TYPES)
        self.assertEqual(
            _read_member(result, "word/_rels/document.xml.rels"), self.GOLDEN_DOCUMENT_RELS
        )

    def test_global_namespace_map_not_leaked_after_call(self):
        snapshot = dict(ET._namespace_map)
        _serialize_xml(ET.Element(f"{{{CONTENT_TYPES_NS}}}Types"), default_ns=CONTENT_TYPES_NS)
        self.assertEqual(
            ET._namespace_map,
            snapshot,
            "image-normalize serialization leaked the empty-prefix registration into "
            "the process-global ET._namespace_map",
        )
        # Specifically, the empty prefix for the OPC content-types uri must be gone
        # (unless it was somehow present before, which it is not by default).
        self.assertNotEqual(ET._namespace_map.get(CONTENT_TYPES_NS), "")

    def test_map_restored_even_when_serialization_raises(self):
        snapshot = dict(ET._namespace_map)
        with mock.patch.object(ET, "tostring", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                _serialize_xml(ET.Element(f"{{{CONTENT_TYPES_NS}}}Types"), default_ns=CONTENT_TYPES_NS)
        self.assertEqual(
            ET._namespace_map,
            snapshot,
            "the global namespace map must be restored in a finally even when "
            "serialization raises mid-registration",
        )

    def test_concurrent_different_default_namespaces_do_not_contaminate(self):
        # Two threads hammer the two FIXED serialization paths that both register
        # the empty prefix for DIFFERENT uris: image-normalize's content-types ns
        # (via _serialize_xml) and OPC relationships ns (via docx_xml._xml_bytes).
        # A barrier forces them to interleave. Without the lock+restore, one
        # thread's leaked empty-prefix registration corrupts the other's output
        # (foreign uri / invented ns0: prefix); with it, each output is clean.
        pre_snapshot = dict(ET._namespace_map)
        iterations = 250
        barrier = threading.Barrier(2)
        errors: list[str] = []
        errors_lock = threading.Lock()

        def record(msg: str) -> None:
            with errors_lock:
                errors.append(msg)

        def image_normalize_worker() -> None:
            root = ET.Element(f"{{{CONTENT_TYPES_NS}}}Types")
            child = ET.SubElement(root, f"{{{CONTENT_TYPES_NS}}}Default")
            child.set("Extension", "png")
            for _ in range(iterations):
                barrier.wait()
                try:
                    out = _serialize_xml(root, default_ns=CONTENT_TYPES_NS)
                except Exception as exc:  # e.g. the KeyError the bulk-clear restore raised
                    record(f"content-types serialization raised {type(exc).__name__}: {exc}")
                    continue
                text = out.decode("utf-8")
                if f'xmlns="{CONTENT_TYPES_NS}"' not in text:
                    record(f"content-types output missing its own default ns: {text!r}")
                if RELATIONSHIPS_NS in text:
                    record(f"content-types output contaminated by relationships ns: {text!r}")
                if "ns0:" in text or "ns1:" in text or "<Relationship" in text:
                    record(f"content-types output carries a foreign prefix/root: {text!r}")

        def opc_relationships_worker() -> None:
            root = ET.Element(f"{{{RELATIONSHIPS_NS}}}Relationships")
            rel = ET.SubElement(root, f"{{{RELATIONSHIPS_NS}}}Relationship")
            rel.set("Id", "rId1")
            for _ in range(iterations):
                barrier.wait()
                try:
                    out = _xml_bytes(root, default_namespace=RELATIONSHIPS_NS)
                except Exception as exc:
                    record(f"relationships serialization raised {type(exc).__name__}: {exc}")
                    continue
                text = out.decode("utf-8")
                if f'xmlns="{RELATIONSHIPS_NS}"' not in text:
                    record(f"relationships output missing its own default ns: {text!r}")
                if CONTENT_TYPES_NS in text:
                    record(f"relationships output contaminated by content-types ns: {text!r}")
                if "ns0:" in text or "ns1:" in text or "<Types" in text:
                    record(f"relationships output carries a foreign prefix/root: {text!r}")

        threads = [
            threading.Thread(target=image_normalize_worker),
            threading.Thread(target=opc_relationships_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"cross-contamination observed under concurrency: {errors[:5]}")
        self.assertEqual(
            ET._namespace_map,
            pre_snapshot,
            "ET._namespace_map was not restored to its pre-test contents after "
            "concurrent scoped registrations",
        )


class ConversionCachingAndBoundsTests(unittest.TestCase):
    """EMF/WMF->PNG normalization runs on the reviewed-docx / source-docx SERVE
    paths, one soffice subprocess per image. Without bounds that is a self-DoS on
    the 1-CPU/2GB box (the same letterhead reconverted every request; unbounded
    concurrency; a 200-image document pinning a request thread for ~100 minutes).
    These tests pin the fix: content-hash cache (in-proc + disk), the SHARED
    soffice semaphore, a per-document conversion cap, and fail-soft."""

    def test_cache_converts_identical_bytes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = _ImageConversionCache(cache_dir=Path(tmp))
            converter = FakeConverter(PNG_SIGNATURE + b"payload")
            docx = _docx_with_emf_media(
                {
                    "word/media/image1.emf": b"SAME-EMF-BYTES",
                    "word/media/image2.emf": b"SAME-EMF-BYTES",
                }
            )
            result = normalize_docx_emf_wmf_images(docx, converter=converter, image_cache=cache)
        # Two parts, identical bytes -> the converter runs exactly ONCE.
        self.assertEqual(len(converter.calls), 1)
        self.assertEqual(sorted(_media_png_names(result)), ["word/media/image1.png", "word/media/image2.png"])

    def test_disk_cache_survives_a_fresh_cache_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            docx = _docx_with_emf_media({"word/media/image1.emf": b"LETTERHEAD-LOGO"})

            first = FakeConverter(PNG_SIGNATURE + b"first-output")
            normalize_docx_emf_wmf_images(
                docx, converter=first, image_cache=_ImageConversionCache(cache_dir=cache_dir)
            )
            self.assertEqual(len(first.calls), 1)

            # A brand-new cache instance over the same dir models a fresh process
            # (empty in-memory tier). The conversion must come off DISK -- the
            # second converter is never invoked.
            second = FakeConverter(PNG_SIGNATURE + b"second-output")
            result = normalize_docx_emf_wmf_images(
                docx, converter=second, image_cache=_ImageConversionCache(cache_dir=cache_dir)
            )
            self.assertEqual(second.calls, [])
            self.assertEqual(_read_member(result, "word/media/image1.png"), PNG_SIGNATURE + b"first-output")

    def test_cache_hits_do_not_count_against_the_per_document_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = _ImageConversionCache(cache_dir=Path(tmp))
            converter = FakeConverter(PNG_SIGNATURE + b"payload")
            docx = _docx_with_emf_media(
                {f"word/media/image{i}.emf": b"IDENTICAL-LOGO" for i in range(3)}
            )
            # Cap of 1 conversion, but all three parts share one hash: 1 convert +
            # 2 free cache hits -> all three normalized, only one conversion spent.
            result = normalize_docx_emf_wmf_images(
                docx, converter=converter, image_cache=cache, max_images=1
            )
        self.assertEqual(len(converter.calls), 1)
        self.assertEqual(len(_media_png_names(result)), 3)
        self.assertEqual(_media_emf_names(result), [])

    def test_per_document_conversion_cap_is_enforced_and_logged(self):
        converter = FakeConverter(PNG_SIGNATURE + b"payload")
        docx = _docx_with_emf_media(
            {f"word/media/image{i}.emf": f"DISTINCT-EMF-{i}".encode() for i in range(5)}
        )
        with self.assertLogs("nda_automation.docx_image_normalize", level="WARNING") as logs:
            result = normalize_docx_emf_wmf_images(
                docx, converter=converter, image_cache=None, max_images=2
            )
        # Only the first two distinct images convert; the remaining three are left
        # un-normalized (still .emf) and a warning is emitted.
        self.assertEqual(len(converter.calls), 2)
        self.assertEqual(len(_media_png_names(result)), 2)
        self.assertEqual(len(_media_emf_names(result)), 3)
        self.assertTrue(any("capped" in line for line in logs.output))

    def test_wall_clock_budget_stops_further_conversions(self):
        # A converter that sleeps past the budget: the first conversion blows the
        # whole wall-clock allowance, so no further images convert.
        def slow(data: bytes, extension: str) -> bytes:
            time.sleep(0.05)
            return PNG_SIGNATURE + b"slow"

        docx = _docx_with_emf_media(
            {f"word/media/image{i}.emf": f"DISTINCT-{i}".encode() for i in range(4)}
        )
        with self.assertLogs("nda_automation.docx_image_normalize", level="WARNING"):
            result = normalize_docx_emf_wmf_images(
                docx, converter=slow, image_cache=None, time_budget_seconds=0.01
            )
        # One conversion is always allowed (budget checked before each); the rest
        # are skipped once the elapsed time exceeds the budget.
        self.assertEqual(len(_media_png_names(result)), 1)
        self.assertEqual(len(_media_emf_names(result)), 3)

    def test_conversion_failure_still_produces_a_valid_docx(self):
        def boom(data: bytes, extension: str) -> bytes:
            raise RuntimeError("converter exploded")

        docx = _docx_with_emf_media({"word/media/image1.emf": b"EMF-BYTES"})
        result = normalize_docx_emf_wmf_images(docx, converter=boom, image_cache=None)
        # The export still succeeds; the un-convertable image becomes the
        # guaranteed placeholder PNG rather than raising or leaving a blank part.
        self.assertTrue(_read_member(result, "word/media/image1.png").startswith(PNG_SIGNATURE))

    def test_default_soffice_path_bounded_to_two_concurrent_children(self):
        # The default converter's soffice conversions must go through the SAME
        # process-wide BoundedSemaphore(2) the DOCX->PDF path uses. Drive it from
        # four threads with distinct images and instrument the soffice runner:
        # concurrency must never exceed two.
        import nda_automation.document_rendering as document_rendering

        state = {"now": 0, "max": 0}
        state_lock = threading.Lock()
        errors: list[str] = []
        errors_lock = threading.Lock()

        def fake_run_soffice(command, *, cwd, timeout_seconds):
            with state_lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            try:
                time.sleep(0.02)
                (Path(cwd) / "image.png").write_bytes(PLACEHOLDER_PNG_BYTES)
                return 0, b"", b""
            finally:
                with state_lock:
                    state["now"] -= 1

        def worker(seed: int) -> None:
            try:
                media = {
                    f"word/media/img{seed}_{i}.emf": f"EMF-{seed}-{i}".encode() for i in range(3)
                }
                # image_cache=None + default converter -> real soffice path (faked
                # at the runner), no caching, so every image actually "converts".
                normalize_docx_emf_wmf_images(_docx_with_emf_media(media), image_cache=None)
            except Exception as exc:  # pragma: no cover - failure path
                with errors_lock:
                    errors.append(repr(exc))

        with mock.patch.object(document_rendering, "_run_soffice_command", side_effect=fake_run_soffice), \
                mock.patch.object(image_normalize.shutil, "which", return_value="/fake/soffice"):
            threads = [threading.Thread(target=worker, args=(s,)) for s in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertGreater(state["max"], 0, "no soffice conversions ran; the test did not exercise the path")
        self.assertLessEqual(
            state["max"], 2, "soffice conversions exceeded the shared BoundedSemaphore(2) cap"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
